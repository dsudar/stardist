from pathlib import Path
from pkg_resources import get_distribution
from itertools import chain
from zipfile import ZipFile
import numpy as np
from csbdeep.utils import axes_check_and_normalize, move_image_axes, normalize, _raise


def _import(error=True):
    try:
        from importlib_metadata import metadata
        from bioimageio.core.build_spec import build_model
    except ImportError:
        if error:
            raise RuntimeError(
                "Required libraries are missing for bioimage.io model export.\n"
                "Please install StarDist as follows: pip install 'stardist[bioimageio]'\n"
                "(You do not need to uninstall StarDist first.)"
            )
        else:
            return None
    return metadata, build_model


def _create_stardist_dependencies(outdir):
    pkg_info = get_distribution("stardist")
    reqs = ("tensorflow",) + tuple(map(str, pkg_info.requires()))
    path = outdir / "requirements.txt"
    with open(path, "w") as f:
        f.write("\n".join(reqs))
    return f"pip:{path}"


def _create_stardist_doc(outdir):
    doc_path = outdir / "README.md"
    text = (
        "# StarDist Model\n"
        "This is a model for object detection with star-convex shapes.\n"
        "Please see the [StarDist repository](https://github.com/stardist/stardist) for details."
    )
    with open(doc_path, "w") as f:
        f.write(text)
    return doc_path


def _get_stardist_metadata(outdir):
    metadata, _ = _import()
    package_data = metadata("stardist")
    doi_2d = "https://doi.org/10.1007/978-3-030-00934-2_30"
    doi_3d = "https://doi.org/10.1109/WACV45572.2020.9093435"
    data = dict(
        description=package_data["Summary"],
        authors=list(dict(name=name.strip()) for name in package_data["Author"].split(",")),
        git_repo=package_data["Home-Page"],
        license=package_data["License"],
        dependencies=_create_stardist_dependencies(outdir),
        cite={"Cell Detection with Star-Convex Polygons": doi_2d,
              "Star-convex Polyhedra for 3D Object Detection and Segmentation in Microscopy": doi_3d},
        tags=["stardist", "segmentation", "instance segmentation", "object detection", "tensorflow"],
        covers=["https://raw.githubusercontent.com/stardist/stardist/master/images/stardist_logo.jpg"],
        documentation=_create_stardist_doc(outdir),
    )
    return data


# TODO factor that out (it's the same as in csbdeep.base_model)
def _get_weights_name(model, prefer="best"):
    # get all weight files and sort by modification time descending (newest first)
    weights_ext = ("*.h5", "*.hdf5")
    weights_files = chain(*(model.logdir.glob(ext) for ext in weights_ext))
    weights_files = reversed(sorted(weights_files, key=lambda f: f.stat().st_mtime))
    weights_files = list(weights_files)
    if len(weights_files) == 0:
        raise ValueError("Couldn't find any network weights (%s) to load." % ', '.join(weights_ext))
    weights_preferred = list(filter(lambda f: prefer in f.name, weights_files))
    weights_chosen = weights_preferred[0] if len(weights_preferred) > 0 else weights_files[0]
    return weights_chosen.name


def _predict_tf(model_path, test_input):
    import tensorflow as tf
    from csbdeep.utils.tf import IS_TF_1
    # need to unzip the model assets
    model_assets = model_path.parent / "tf_model"
    with ZipFile(model_path, "r") as f:
        f.extractall(model_assets)
    if IS_TF_1:
        # make a new graph, i.e. don't use the global default graph
        with tf.Graph().as_default():
            with tf.Session() as sess:
                tf_model = tf.saved_model.load_v2(str(model_assets))
                x = tf.convert_to_tensor(test_input, dtype=tf.float32)
                model = tf_model.signatures["serving_default"]
                y = model(x)
                sess.run(tf.global_variables_initializer())
                output = sess.run(y["output"])
    else:
        tf_model = tf.saved_model.load(str(model_assets))
        x = tf.convert_to_tensor(test_input, dtype=tf.float32)
        model = tf_model.signatures["serving_default"]
        y = model(x)
        output = y["output"].numpy()
    return output


def _get_weights_and_model_metadata(
    outdir, model, test_input, input_axes, mode, prefer_weights, min_percentile, max_percentile
):

    # get the path to the weights
    weights_name = _get_weights_name(model, prefer_weights)
    if mode == "keras_hdf5":
        raise NotImplementedError("Export to keras format is not supported yet")
        weight_uri = model.logdir / weights_name
    elif mode == "tensorflow_saved_model_bundle":
        weight_uri = model.logdir / "TF_SavedModel.zip"
        model.load_weights(weights_name)
        model_csbdeep = model.export_TF(weight_uri, single_output=True, upsample_grid=True)
    else:
        raise ValueError(f"Unsupported mode: {mode}")

    # TODO: this needs more attention, e.g. how axes are treated in a general way
    axes = model.config.axes.lower()
    # img_axes_in = axes_check_and_normalize(axes, model.config.n_dim+1)
    net_axes_in = axes
    net_axes_out = axes_check_and_normalize(model._axes_out).lower()
    # net_axes_lost = set(net_axes_in).difference(set(net_axes_out))
    # img_axes_out = ''.join(a for a in img_axes_in if a not in net_axes_lost)

    ndim_tensor = model.config.n_dim + 2

    # input shape including batch size
    div_by = list(model._axes_div_by(net_axes_in))

    if mode == "keras_hdf5":
        output_names = ("prob", "dist") + (("class_prob",) if model._is_multiclass() else ())
        output_n_channels = (1, model.config.n_rays,) + ((1,) if model._is_multiclass() else ())
        output_scale = [1]+list(1/g for g in model.config.grid) + [0]

    elif mode == "tensorflow_saved_model_bundle":
        if model._is_multiclass():
            raise NotImplementedError("Tensorflow SavedModel not supported for multiclass models yet")
        # output_names = ("outputall",)
        # output_n_channels = (1 + model.config.n_rays,)
        # output_scale = [1]*(ndim_tensor-1) + [0]

        # output_names = ("prob",)
        # output_n_channels = (1,)
        # output_scale = [1]*(ndim_tensor-1) + [0]

        input_names = model_csbdeep.input_names
        # NOTE model_csbdeep.output_names returns the wrong value; this needs to be the key that is passed to signature[signature_key].outputs[key]:
        # https://github.com/bioimage-io/core-bioimage-io-python/blob/main/bioimageio/core/prediction_pipeline/_model_adapters/_tensorflow_model_adapter.py#L69
        # which is "output".Iinstead, output_names is ["concatenate_4"].
        # output_names = model_csbdeep.output_names
        output_names = ["output"]

        output_n_channels = (1 + model.config.n_rays,)
        output_scale = [1]*(ndim_tensor-1) + [0]

    # TODO need config format that is compatible with deepimagej; discuss with Esti
    # TODO do we need parameters for down/upsampling here?
    metadata, _ = _import()
    package_data = metadata("stardist")
    config = dict(
        stardist=dict(
            stardist_version=package_data["Version"],
            thresholds=dict(nms=model.thresholds.nms, prob=model.thresholds.prob)
        )
    )

    n_inputs = len(input_names)
    assert n_inputs == 1
    # the input axes according to the network (csbdeep convention)
    csbdeep_input_axes = "S" + net_axes_in.lower()
    bioimageio_input_axes = csbdeep_input_axes.replace("S", "B").lower()
    input_config = dict(
        input_name=input_names,
        input_step=[[0]+div_by] * n_inputs,
        input_min_shape=[[1] + div_by] * n_inputs,
        input_axes=[bioimageio_input_axes] * n_inputs,
        input_data_range=[["-inf", "inf"]] * n_inputs,
        preprocessing=[dict(scale_range=dict(
            mode="per_sample",
            # TODO might make it an option to normalize across channels ...
            axes=net_axes_in.lower().replace("c", ""),
            min_percentile=min_percentile,
            max_percentile=max_percentile,
        ))] * n_inputs
    )

    n_outputs = len(output_names)
    assert len(output_n_channels) == n_outputs
    output_axes = net_axes_out
    csbdeep_output_axes = "S" + output_axes
    bioimageio_output_axes = csbdeep_output_axes.replace("S", "B").lower()
    output_config = dict(
        output_name=output_names,
        output_data_range=[["-inf", "inf"]] * n_outputs,
        output_axes=[bioimageio_output_axes] * n_outputs,
        output_reference=[input_names[0]] * n_outputs,
        output_scale=[output_scale] * n_outputs,
        output_offset=[[1] * (ndim_tensor-1) + [n_channel] for n_channel in output_n_channels]
    )

    in_path = outdir / "test_input.npy"
    np.save(in_path, move_image_axes(test_input, input_axes.upper(), csbdeep_input_axes))

    test_input = normalize(test_input, pmin=min_percentile, pmax=max_percentile)
    if mode == "tensorflow_saved_model_bundle":
        test_outputs = _predict_tf(weight_uri, move_image_axes(test_input, input_axes.upper(), csbdeep_input_axes))
    else:
        test_outputs = model.predict(test_input)

    out_paths = []
    for i, out in enumerate(test_outputs):
        p = outdir / f"test_output{i}.npy"
        np.save(p, move_image_axes(out, output_axes, bioimageio_output_axes))
        out_paths.append(p)

    data = dict(weight_uri=weight_uri, test_inputs=[in_path], test_outputs=out_paths, config=config)
    data.update(input_config)
    data.update(output_config)
    return data


def export_bioimageio(
    model,
    outpath,
    test_input,
    input_axes,
    name="bioimageio_model",
    mode="tensorflow_saved_model_bundle",
    prefer_weights="best",
    min_percentile=1.0,
    max_percentile=99.8,
    overwrite_spec_kwargs={}
):
    """Export stardist model into bioimageio format, https://github.com/bioimage-io/spec-bioimage-io.

    Parameters
    ----------
    model: StarDist2D, StarDist3D
        the model to convert
    outpath: str, Path
        where to save the model
    test_input: np.ndarray
        input image for generating test data
    input_axes: str
        the axes of the test input, for example 'YX' for a 2d image or 'ZYX' for a 3d volume
    name: str
        the name of this model (default: "StarDist Model")
    mode: str
        the weight type for this model (default: "tensorflow_saved_model_bundle")
    prefer_weights: str
        the checkpoint to be loaded (default: "best")
    overwrite_spec_kwargs: dict
        spec keywords that should be overloaded (default: {})
    """
    _, build_model = _import()
    from stardist.models import StarDist2D, StarDist3D
    isinstance(model, (StarDist2D, StarDist3D)) or _raise(ValueError("not a valid model"))
    0 <= min_percentile < max_percentile <= 100 or _raise(ValueError("invalid percentile values"))

    outpath = Path(outpath)
    if outpath.suffix == "":
        outdir = outpath
        zip_path = outdir / f"{name}.zip"
    elif outpath.suffix == ".zip":
        outdir = outpath.parent
        zip_path = outpath
    else:
        raise ValueError(f"outpath has to be a folder or zip file, got {outpath}")
    outdir.mkdir(exist_ok=True, parents=True)

    kwargs = _get_stardist_metadata(outdir)
    model_kwargs = _get_weights_and_model_metadata(outdir, model, test_input, input_axes, mode, prefer_weights,
                                                   min_percentile=min_percentile, max_percentile=max_percentile)
    kwargs.update(model_kwargs)
    kwargs.update(overwrite_spec_kwargs)

    build_model(name=name, output_path=zip_path, **kwargs)
