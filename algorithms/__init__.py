__all__ = [
    "transfer_color",
    "ALGORITHMS",
    "generate_3dlut_from_reinhard",
    "apply_3dlut",
    "evaluate_transfer",
    "neural_preset_transfer",
    "modflows_transfer",
    "process_video",
    "enhance_transfer_result",
    "regional_transfer",
    "full_segmentation",
    "visualize_segmentation",
]


def __getattr__(name):
    if name in ("transfer_color", "ALGORITHMS", "generate_3dlut_from_reinhard", "apply_3dlut"):
        from .color_transfer import ALGORITHMS, apply_3dlut, generate_3dlut_from_reinhard, transfer_color
        return {
            "transfer_color": transfer_color,
            "ALGORITHMS": ALGORITHMS,
            "generate_3dlut_from_reinhard": generate_3dlut_from_reinhard,
            "apply_3dlut": apply_3dlut,
        }[name]
    if name == "evaluate_transfer":
        from .metrics import evaluate_transfer
        return evaluate_transfer
    if name == "neural_preset_transfer":
        from .neural_preset import neural_preset_transfer
        return neural_preset_transfer
    if name == "modflows_transfer":
        from .modflows import modflows_transfer
        return modflows_transfer
    if name == "process_video":
        from .video import process_video
        return process_video
    if name in ("enhance_transfer_result", "regional_transfer"):
        from .postprocess import enhance_transfer_result, regional_transfer
        return {
            "enhance_transfer_result": enhance_transfer_result,
            "regional_transfer": regional_transfer,
        }[name]
    if name in ("full_segmentation", "visualize_segmentation"):
        from .segmentation import full_segmentation, visualize_segmentation
        return {
            "full_segmentation": full_segmentation,
            "visualize_segmentation": visualize_segmentation,
        }[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
