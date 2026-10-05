"""Image metadata provenance signal (planning.md §9.4). Pure Python, no network."""

import re

from .scoring import clamp

NAME = "metadata"

AI_GENERATORS = [
    "midjourney", "dall-e", "dall·e", "dalle", "stable diffusion", "stablediffusion", "sdxl", "firefly",
    "comfyui", "automatic1111", "a1111", "leonardo", "ideogram", "flux", "novelai", "invokeai",
    "dreamstudio", "imagen", "runway",
]
TOOL_KEYS = ["software", "creator_tool", "creatortool", "generator", "application", "processing_software"]
GEN_PARAM_KEYS = ["parameters", "prompt", "negative_prompt", "seed", "cfg_scale", "sampler", "steps"]
CAMERA_EXPOSURE_KEYS = ["exposure_time", "exposuretime", "f_number", "fnumber", "aperture", "iso",
                        "iso_speed", "focal_length", "focallength", "shutter_speed"]
AI_SOURCE_TYPES = ["trainedalgorithmicmedia", "compositesynthetic", "algorithmicmedia"]
GENERATOR_SIZES = {512, 768, 1024, 1536, 2048}


def _norm_keys(metadata):
    return {re.sub(r"[\s\-]+", "_", str(k).strip().lower()): v for k, v in (metadata or {}).items()}


def _as_int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def metadata_signal(metadata):
    md = _norm_keys(metadata if isinstance(metadata, dict) else {})
    evidence = []
    score = None

    source_type = str(md.get("digital_source_type", "") or md.get("digitalsourcetype", "")).lower()
    tool_text = " ".join(str(md.get(k, "")) for k in TOOL_KEYS).lower()
    generator = next((g for g in AI_GENERATORS if g in tool_text), None)
    param_keys = [k for k in GEN_PARAM_KEYS if k in md and md[k] not in (None, "")]
    has_camera = bool(md.get("make")) and bool(md.get("model"))
    exposure_keys = [k for k in CAMERA_EXPOSURE_KEYS if k in md and md[k] not in (None, "")]

    if any(t in source_type for t in AI_SOURCE_TYPES):
        score = 0.97
        evidence.append(f"IPTC digital_source_type = {source_type!r}")
    elif generator:
        score = 0.95
        evidence.append(f"AI generator named in software/creator tool: {generator!r}")
    elif param_keys:
        score = 0.90
        evidence.append(f"generation parameters present: {param_keys}")
    elif has_camera and exposure_keys:
        score = 0.15
        evidence.append(f"camera {md.get('make')} {md.get('model')} with exposure data {exposure_keys}")
    elif has_camera:
        # Spec only scores camera + exposure; make/model alone is weaker (easy to type), so lean human less.
        score = 0.30
        evidence.append(f"camera make/model {md.get('make')} {md.get('model')} but no exposure data")
    else:
        score = 0.5
        evidence.append("no provenance evidence (metadata missing or stripped)")

    w, h = _as_int(md.get("width")), _as_int(md.get("height"))
    square_generator_size = bool(w and h and w == h and (w in GENERATOR_SIZES or w % 64 == 0))
    if square_generator_size:
        score = clamp(score + 0.10)
        evidence.append(f"generator-typical square size {w}x{h} (+0.10)")

    return {
        "name": NAME,
        "score": round(score, 3),
        "available": True,
        "details": {
            "evidence": evidence,
            "generator_match": generator,
            "generation_param_keys": param_keys,
            "camera": f"{md.get('make')} {md.get('model')}" if has_camera else None,
            "exposure_keys": exposure_keys,
            "digital_source_type": source_type or None,
            "dimensions": [w, h] if w and h else None,
            "square_generator_size": square_generator_size,
            "metadata_field_count": len(md),
        },
    }
