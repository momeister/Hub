"""
skills/comfyui/skill.py — ComfyUI Image Generation v3
======================================================
Loads workflow files from skills/comfyui/workflows/*.json,
auto-converts UI format to API format (including subgraph expansion),
injects dynamic params, queues the job, and downloads results.

Supports both:
  - API format (flat dict with node IDs as keys)
  - UI format  (has "nodes" array, "links", "definitions" — regular Save)

ComfyUI auto-starts if not running and COMFYUI_PATH is configured.
"""

import copy
import json
import logging
import os
import random
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

log = logging.getLogger("skill.comfyui")

COMFYUI_URL = os.environ.get("COMFYUI_URL", "http://localhost:8188")
COMFYUI_PATH = os.environ.get(
    "COMFYUI_PATH",
    r"C:\ComfyUI_windows_portable_nvidia\ComfyUI_windows_portable",
)
WORKFLOWS_DIR = os.path.join(os.path.dirname(__file__), "workflows")

DEFAULT_STEPS = 20
DEFAULT_WIDTH = 1248
DEFAULT_HEIGHT = 821
DEFAULT_CFG = 7.0

# Smart dispatch: workflow categories
# Flux workflows require a reference image (img2img / modification)
# Z_Turbo workflows are text-to-image (generate from scratch)
# *_backgroundRemove variants add transparent background post-processing
WORKFLOW_CATEGORIES = {
    "img2img": "image_flux2",
    "img2img_nobg": "image_flux2_baackgroundRemove",
    "txt2img": "image_z_image_turbo",
    "txt2img_nobg": "image_z_image_turbo_backgroundRemove",
}

# Keywords that indicate transparent background / sprite request
NOBG_KEYWORDS = (
    "transparent", "no background", "remove background", "background remove",
    "nobg", "no bg", "sprite", "cutout", "isolated", "png transparent",
    "sticker", "icon",
)

# Track ComfyUI process so we don't start it twice
_comfyui_process: Optional[subprocess.Popen] = None


# ============================================================================
# COMFYUI AUTO-START
# ============================================================================

def _check_comfyui() -> bool:
    """Check if ComfyUI is reachable."""
    try:
        req = urllib.request.Request(f"{COMFYUI_URL}/system_stats", method="GET")
        with urllib.request.urlopen(req, timeout=5):
            return True
    except Exception:
        return False


def _ensure_comfyui_running(timeout: int = 120) -> bool:
    """
    Ensure ComfyUI is running. If not, start it automatically.
    Returns True if ComfyUI is reachable after this call.
    """
    global _comfyui_process

    if _check_comfyui():
        return True

    # Try to start ComfyUI
    comfyui_dir = Path(COMFYUI_PATH)
    if not comfyui_dir.exists():
        log.error(f"COMFYUI_PATH not found: {COMFYUI_PATH}")
        return False

    python_exe = comfyui_dir / "python_embeded" / "python.exe"
    main_py = comfyui_dir / "ComfyUI" / "main.py"

    if not python_exe.exists() or not main_py.exists():
        log.error(f"ComfyUI files not found: python={python_exe}, main={main_py}")
        return False

    # Don't start if we already have a process running
    if _comfyui_process is not None and _comfyui_process.poll() is None:
        log.info("ComfyUI process already started, waiting...")
    else:
        log.info(f"Starting ComfyUI from {comfyui_dir}...")
        try:
            _comfyui_process = subprocess.Popen(
                [str(python_exe), "-s", str(main_py), "--windows-standalone-build"],
                cwd=str(comfyui_dir),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW
                if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
            )
            log.info(f"ComfyUI started (PID {_comfyui_process.pid})")
        except Exception as e:
            log.error(f"Failed to start ComfyUI: {e}")
            return False

    # Wait for it to become reachable
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _check_comfyui():
            log.info("ComfyUI is now reachable")
            return True
        time.sleep(3)

    log.error(f"ComfyUI did not start within {timeout}s")
    return False


# ============================================================================
# UI FORMAT → API FORMAT CONVERTER
# ============================================================================

def _is_ui_format(wf: dict) -> bool:
    """Detect if a workflow JSON is in UI format (vs API format)."""
    return "nodes" in wf and isinstance(wf.get("nodes"), list)


def _parse_links(links_raw: list) -> dict:
    """
    Parse links from either format:
      - Array: [link_id, origin_id, origin_slot, target_id, target_slot, ...]
      - Dict:  {"id":..., "origin_id":..., "origin_slot":..., ...}
    Returns {link_id: (origin_id, origin_slot, target_id, target_slot)}
    """
    link_map = {}
    for link in links_raw:
        if isinstance(link, list) and len(link) >= 5:
            link_map[link[0]] = (link[1], link[2], link[3], link[4])
        elif isinstance(link, dict):
            link_map[link["id"]] = (
                link["origin_id"], link["origin_slot"],
                link["target_id"], link["target_slot"],
            )
    return link_map


def _build_node_inputs(node: dict, link_map: dict) -> dict:
    """
    Build API-format inputs dict from a UI-format node.
    Maps widget values and link connections to named inputs.
    """
    inputs = {}
    widget_vals = list(node.get("widgets_values") or [])
    w_idx = 0

    for inp in node.get("inputs", []):
        name = inp.get("name", "")
        link_id = inp.get("link")
        has_widget = "widget" in inp

        if link_id is not None and link_id in link_map:
            origin_id, origin_slot, _, _ = link_map[link_id]
            inputs[name] = [str(origin_id), origin_slot]
            if has_widget:
                w_idx += 1  # skip widget value for linked input
        elif has_widget:
            if w_idx < len(widget_vals):
                inputs[name] = widget_vals[w_idx]
            w_idx += 1

    return inputs


def _expand_subgraph(
    api: dict,
    instance_node: dict,
    sg_def: dict,
    parent_link_map: dict,
    output_remap: dict,
):
    """
    Expand a subgraph instance into individual API-format nodes.

    - Remaps internal node IDs to be globally unique (offset by instance_id * 1000)
    - Connects subgraph virtual input node (-10) to external sources
    - Registers subgraph output mappings so downstream nodes can be patched
    """
    instance_id = instance_node["id"]
    sg_nodes = sg_def.get("nodes", [])
    sg_links_raw = sg_def.get("links", [])
    offset = instance_id * 1000

    # Build internal link map
    sg_link_map = _parse_links(sg_links_raw)

    # Map external connections into the subgraph
    # Instance inputs correspond to subgraph virtual input node (-10) outputs
    external_input_map = {}  # slot_index → [origin_id_str, origin_slot] or scalar
    widget_vals = list(instance_node.get("widgets_values") or [])
    w_idx = 0
    for i, inp in enumerate(instance_node.get("inputs", [])):
        link_id = inp.get("link")
        has_widget = "widget" in inp
        if link_id is not None and link_id in parent_link_map:
            origin_id, origin_slot, _, _ = parent_link_map[link_id]
            external_input_map[i] = [str(origin_id), origin_slot]
            if has_widget:
                w_idx += 1
        elif has_widget:
            if w_idx < len(widget_vals):
                external_input_map[i] = widget_vals[w_idx]
            w_idx += 1

    # Find internal nodes that connect to subgraph output (-20)
    # and register remapping so downstream nodes can find them
    for ldata in sg_link_map.values():
        origin_id, origin_slot, target_id, target_slot = ldata
        if target_id == -20:
            output_remap[(instance_id, target_slot)] = (origin_id + offset, origin_slot)

    # Process each internal node
    for sn in sg_nodes:
        sn_id = sn.get("id")
        sn_type = sn.get("type", "")
        new_id = str(sn_id + offset)

        # Skip muted or non-functional nodes
        if sn.get("mode") == 4 or sn_type in ("MarkdownNote", "Note"):
            continue

        api_inputs = {}
        s_widget_vals = list(sn.get("widgets_values") or [])
        sw_idx = 0

        for sinp in sn.get("inputs", []):
            name = sinp.get("name", "")
            link_id = sinp.get("link")
            has_widget = "widget" in sinp

            if link_id is not None and link_id in sg_link_map:
                origin_id, origin_slot, _, _ = sg_link_map[link_id]

                if origin_id == -10:
                    # Connected to subgraph virtual input → resolve to external
                    if origin_slot in external_input_map:
                        ext = external_input_map[origin_slot]
                        if isinstance(ext, list):
                            api_inputs[name] = list(ext)  # copy
                        else:
                            api_inputs[name] = ext
                    elif has_widget and sw_idx < len(s_widget_vals):
                        api_inputs[name] = s_widget_vals[sw_idx]
                else:
                    # Connected to another internal node
                    api_inputs[name] = [str(origin_id + offset), origin_slot]

                if has_widget:
                    sw_idx += 1
            elif has_widget:
                if sw_idx < len(s_widget_vals):
                    api_inputs[name] = s_widget_vals[sw_idx]
                sw_idx += 1

        api[new_id] = {"class_type": sn_type, "inputs": api_inputs}


def _convert_ui_to_api(ui_wf: dict) -> dict:
    """
    Convert a ComfyUI UI-format workflow JSON to API format.
    Handles regular nodes and subgraph expansion.
    """
    nodes = ui_wf.get("nodes", [])
    links_raw = ui_wf.get("links", [])
    definitions = ui_wf.get("definitions", {})

    # Build subgraph lookup by UUID
    sg_defs = {}
    for sg in definitions.get("subgraphs", []):
        sg_defs[sg["id"]] = sg

    # Parse parent links
    parent_link_map = _parse_links(links_raw)

    api = {}
    output_remap = {}  # (instance_id, slot) → (expanded_node_id, slot)

    for node in nodes:
        nid = node.get("id")
        ntype = node.get("type", "")

        # Skip muted/bypassed (mode 4) and note nodes
        if node.get("mode") == 4 or ntype in ("MarkdownNote", "Note"):
            continue

        if ntype in sg_defs:
            # Expand subgraph instance
            _expand_subgraph(api, node, sg_defs[ntype], parent_link_map, output_remap)
        else:
            # Regular node
            inputs = _build_node_inputs(node, parent_link_map)
            api[str(nid)] = {"class_type": ntype, "inputs": inputs}

    # Patch references to subgraph instances → point to expanded internal nodes
    for nid_str, api_node in api.items():
        for input_name, input_val in list(api_node.get("inputs", {}).items()):
            if isinstance(input_val, list) and len(input_val) == 2:
                try:
                    orig_id = int(input_val[0])
                    orig_slot = input_val[1]
                    if (orig_id, orig_slot) in output_remap:
                        new_id, new_slot = output_remap[(orig_id, orig_slot)]
                        api_node["inputs"][input_name] = [str(new_id), new_slot]
                except (ValueError, TypeError):
                    pass

    log.info(f"Converted UI workflow: {len(nodes)} UI nodes → {len(api)} API nodes")
    return api


# ============================================================================
# WORKFLOW DISCOVERY
# ============================================================================

def discover_workflows() -> dict[str, dict]:
    """
    Scan workflows/ directory for .json files.
    Auto-converts UI format to API format.
    Returns {name: workflow_dict} where name = filename without .json.
    """
    workflows = {}
    wf_dir = Path(WORKFLOWS_DIR)
    if not wf_dir.exists():
        log.warning(f"Workflows directory not found: {WORKFLOWS_DIR}")
        return workflows

    for f in wf_dir.glob("*.json"):
        try:
            with open(f, "r", encoding="utf-8") as fh:
                wf = json.load(fh)
            if not isinstance(wf, dict) or not wf:
                continue

            name = f.stem

            # Auto-convert UI format to API format
            if _is_ui_format(wf):
                log.info(f"Converting UI format workflow: {name}")
                wf = _convert_ui_to_api(wf)

            # Validate: API format should have at least one node with class_type
            has_nodes = any(
                isinstance(v, dict) and "class_type" in v
                for v in wf.values()
            )
            if has_nodes:
                workflows[name] = wf
                log.info(f"Loaded workflow: {name} ({len(wf)} nodes)")
            else:
                log.warning(f"Workflow {name} has no valid nodes, skipping")

        except Exception as e:
            log.warning(f"Failed to load workflow {f.name}: {e}")

    return workflows


def get_available_models() -> list[str]:
    """Return list of available model names (workflow filenames)."""
    return sorted(discover_workflows().keys())


# ============================================================================
# SMART DISPATCH — Auto-select workflow based on intent
# ============================================================================

def detect_intent(
    prompt: str,
    model: str = "",
    reference_image_path: str = "",
) -> str:
    """
    Detect user intent and return the best workflow name.

    Logic:
      1. If user explicitly chose a model, use it.
      2. If reference image provided → img2img (Flux).
      3. If no reference image → txt2img (Z_Turbo).
      4. If background-remove keywords detected → *_backgroundRemove variant.

    Returns: workflow name string.
    """
    if model:
        return model  # User explicitly chose

    prompt_lower = prompt.lower()
    wants_nobg = any(kw in prompt_lower for kw in NOBG_KEYWORDS)
    has_reference = bool(reference_image_path and os.path.isfile(reference_image_path))

    if has_reference:
        category = "img2img_nobg" if wants_nobg else "img2img"
    else:
        category = "txt2img_nobg" if wants_nobg else "txt2img"

    workflow_name = WORKFLOW_CATEGORIES.get(category, "image_z_image_turbo")

    # Verify workflow file actually exists
    available = discover_workflows()
    if workflow_name not in available:
        # Fallback: try without _nobg
        fallback = WORKFLOW_CATEGORIES.get(category.replace("_nobg", ""), "")
        if fallback and fallback in available:
            log.warning(f"Workflow {workflow_name} not found, falling back to {fallback}")
            workflow_name = fallback
        elif available:
            workflow_name = next(iter(available))
            log.warning(f"Fallback to first available workflow: {workflow_name}")

    log.info(f"Smart dispatch: intent={category}, workflow={workflow_name}")
    return workflow_name


def auto_resolution(
    width: int,
    height: int,
    prompt: str = "",
) -> tuple[int, int]:
    """
    Auto-adjust resolution:
      - Default 1248x821 (landscape).
      - If prompt contains portrait-indicating keywords, swap to 821x1248.
      - If user provided explicit dimensions, respect them.

    Returns: (width, height)
    """
    # If user explicitly set non-default dimensions, keep them
    if (width, height) not in ((DEFAULT_WIDTH, DEFAULT_HEIGHT), (0, 0)):
        return width, height

    # Default landscape
    w, h = DEFAULT_WIDTH, DEFAULT_HEIGHT

    # Check for portrait keywords
    portrait_keywords = (
        "portrait", "vertical", "tall", "person", "face", "headshot",
        "character", "standing", "full body", "selfie", "hochformat",
    )
    prompt_lower = prompt.lower()
    if any(kw in prompt_lower for kw in portrait_keywords):
        w, h = h, w  # Swap to portrait (821x1248)
        log.info(f"Auto-resolution: portrait detected -> {w}x{h}")

    return w, h


# ============================================================================
# PARAMETER INJECTION
# ============================================================================

def _trace_to_clip_encode(
    workflow: dict, start_id: str, follow_keys: tuple[str, ...]
) -> Optional[str]:
    """
    Follow a chain of nodes starting from start_id, looking at follow_keys
    in each node's inputs, until we reach a CLIPTextEncode node.
    Returns the CLIPTextEncode node ID, or None if not found.
    Handles chains like: BasicGuider → ReferenceLatent → FluxGuidance → CLIPTextEncode
    or: KSamplerAdvanced → WanImageToVideo → CLIPTextEncode
    """
    visited = set()
    current_id = start_id

    for _ in range(10):  # max 10 hops
        if current_id in visited:
            break
        visited.add(current_id)

        node = workflow.get(current_id, {})
        if not isinstance(node, dict):
            break

        ct = node.get("class_type", "")
        if ct == "CLIPTextEncode":
            return current_id

        # Try each follow key to find the next node in the chain
        inputs = node.get("inputs", {})
        found_next = False
        for key in follow_keys:
            ref = inputs.get(key)
            if isinstance(ref, list) and len(ref) >= 1:
                current_id = str(ref[0])
                found_next = True
                break

        if not found_next:
            break

    return None


def _find_positive_clip_node(workflow: dict) -> Optional[str]:
    """Find the CLIPTextEncode node used for positive prompt."""
    sampler_types = (
        "KSampler", "KSamplerAdvanced",
        "SamplerCustom", "SamplerCustomAdvanced",
        "BasicGuider",
    )
    for node_id, node in workflow.items():
        if not isinstance(node, dict):
            continue
        ct = node.get("class_type", "")
        if ct in sampler_types:
            inputs = node.get("inputs", {})
            for key in ("positive", "conditioning"):
                ref = inputs.get(key)
                if isinstance(ref, list) and len(ref) >= 1:
                    result = _trace_to_clip_encode(
                        workflow, str(ref[0]),
                        ("conditioning", "positive"),
                    )
                    if result:
                        return result
    # Fallback: first CLIPTextEncode node
    for node_id, node in workflow.items():
        if isinstance(node, dict) and node.get("class_type") == "CLIPTextEncode":
            return node_id
    return None


def _find_negative_clip_node(workflow: dict) -> Optional[str]:
    """Find the CLIPTextEncode node used for negative prompt."""
    sampler_types = (
        "KSampler", "KSamplerAdvanced",
        "SamplerCustom", "SamplerCustomAdvanced",
    )
    for node_id, node in workflow.items():
        if not isinstance(node, dict):
            continue
        ct = node.get("class_type", "")
        if ct in sampler_types:
            inputs = node.get("inputs", {})
            neg_ref = inputs.get("negative")
            if isinstance(neg_ref, list) and len(neg_ref) >= 1:
                result = _trace_to_clip_encode(
                    workflow, str(neg_ref[0]),
                    ("conditioning", "negative"),
                )
                if result:
                    return result
    return None


def _inject_params(
    workflow: dict,
    prompt: str = "",
    negative: str = "",
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    steps: int = DEFAULT_STEPS,
    seed: int = -1,
    batch_size: int = 1,
) -> dict:
    """
    Inject dynamic parameters into a workflow template.
    Walks all nodes and modifies known class_types.
    """
    wf = copy.deepcopy(workflow)

    if seed < 0:
        seed = random.randint(0, 2**63 - 1)

    # Find positive/negative CLIP nodes via sampler connections
    pos_node_id = _find_positive_clip_node(wf)
    neg_node_id = _find_negative_clip_node(wf)

    for node_id, node in wf.items():
        if not isinstance(node, dict):
            continue

        ct = node.get("class_type", "")
        inputs = node.get("inputs", {})

        # --- Prompt injection ---
        if ct == "CLIPTextEncode":
            if prompt and node_id == pos_node_id:
                inputs["text"] = prompt
            elif negative and node_id == neg_node_id:
                inputs["text"] = negative

        # --- Latent image size (standard + Flux2) ---
        elif ct in ("EmptyLatentImage", "EmptyFlux2LatentImage"):
            # Only set if not linked (linked means size comes from elsewhere)
            if not isinstance(inputs.get("width"), list):
                inputs["width"] = width
            if not isinstance(inputs.get("height"), list):
                inputs["height"] = height
            if not isinstance(inputs.get("batch_size"), list):
                inputs["batch_size"] = batch_size

        # --- Scheduler (Flux2Scheduler has steps) ---
        elif ct == "Flux2Scheduler":
            if not isinstance(inputs.get("steps"), list):
                inputs["steps"] = steps

        # --- Standard sampler params ---
        elif ct in ("KSampler", "KSamplerAdvanced"):
            if not isinstance(inputs.get("steps"), list):
                inputs["steps"] = steps
            if not isinstance(inputs.get("seed"), list):
                inputs["seed"] = seed
            if "noise_seed" in inputs and not isinstance(inputs.get("noise_seed"), list):
                inputs["noise_seed"] = seed

        # --- Random noise seed ---
        elif ct == "RandomNoise":
            if not isinstance(inputs.get("noise_seed"), list):
                inputs["noise_seed"] = seed

        # --- Custom sampler (may have steps/seed) ---
        elif ct in ("SamplerCustom", "SamplerCustomAdvanced"):
            if "steps" in inputs and not isinstance(inputs["steps"], list):
                inputs["steps"] = steps
            if "noise_seed" in inputs and not isinstance(inputs["noise_seed"], list):
                inputs["noise_seed"] = seed
            elif "seed" in inputs and not isinstance(inputs["seed"], list):
                inputs["seed"] = seed

        # --- Save image prefix ---
        elif ct == "SaveImage":
            inputs["filename_prefix"] = "ai_hub"

        # --- Save video prefix ---
        elif ct == "SaveVideo":
            inputs["filename_prefix"] = "ai_hub"

        # --- WAN image-to-video (width, height, batch_size) ---
        elif ct == "WanImageToVideo":
            if not isinstance(inputs.get("width"), list):
                inputs["width"] = width
            if not isinstance(inputs.get("height"), list):
                inputs["height"] = height
            if not isinstance(inputs.get("batch_size"), list):
                inputs["batch_size"] = batch_size

    return wf


def _inject_reference_image(workflow: dict, image_name: str) -> dict:
    """If workflow has a LoadImage node, set its image to the uploaded filename."""
    for node_id, node in workflow.items():
        if isinstance(node, dict) and node.get("class_type") == "LoadImage":
            node.get("inputs", {})["image"] = image_name
            break
    return workflow


# ============================================================================
# COMFYUI API
# ============================================================================

def _upload_image(image_path: str) -> Optional[str]:
    """Upload a reference image to ComfyUI's input directory."""
    boundary = "----ComfyUploadBoundary"
    filename = os.path.basename(image_path)

    with open(image_path, "rb") as f:
        file_data = f.read()

    body_parts = [
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="image"; filename="{filename}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n"
    ]
    body = body_parts[0].encode() + file_data + f"\r\n--{boundary}--\r\n".encode()

    try:
        req = urllib.request.Request(
            f"{COMFYUI_URL}/upload/image",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
            return result.get("name", filename)
    except Exception as e:
        log.error(f"Image upload failed: {e}")
        return None


def _queue_prompt(workflow: dict) -> str:
    """Send workflow to ComfyUI queue, return prompt_id."""
    data = json.dumps({"prompt": workflow}).encode()
    req = urllib.request.Request(
        f"{COMFYUI_URL}/prompt",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read())
    return result["prompt_id"]


def _get_queue_status(prompt_id: str) -> str:
    """Check ComfyUI queue for prompt status."""
    try:
        url = f"{COMFYUI_URL}/queue"
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read())
        for item in data.get("queue_running", []):
            if len(item) >= 2 and item[1] == prompt_id:
                return "generating"
        for i, item in enumerate(data.get("queue_pending", [])):
            if len(item) >= 2 and item[1] == prompt_id:
                return f"queued (#{i + 1})"
        return "processing"
    except Exception:
        return "processing"


def _wait_for_result(
    prompt_id: str,
    timeout: int = 1800,
    progress_callback=None,
) -> dict:
    """
    Poll ComfyUI history until job completes. Returns history entry.
    Default timeout 1800s (30 min) to allow for large video generation.
    Progress callback fires at 25%, 50%, 75% only (reduces Telegram spam).
    """
    deadline = time.time() + timeout
    start_time = time.time()
    reported_milestones = set()

    while time.time() < deadline:
        try:
            url = f"{COMFYUI_URL}/history/{prompt_id}"
            with urllib.request.urlopen(url, timeout=10) as resp:
                history = json.loads(resp.read())
            if prompt_id in history:
                return history[prompt_id]
        except Exception as e:
            log.debug(f"History poll error: {e}")

        # Percentage-based progress: only report at 25%, 50%, 75%
        if progress_callback:
            elapsed = time.time() - start_time
            pct = min(int((elapsed / timeout) * 100), 99)

            for milestone in (25, 50, 75):
                if pct >= milestone and milestone not in reported_milestones:
                    reported_milestones.add(milestone)
                    status = _get_queue_status(prompt_id)
                    progress_callback(int(elapsed), status, milestone)
                    break

        time.sleep(3)
    raise TimeoutError(f"ComfyUI timeout after {timeout}s")


def _download_outputs(history_entry: dict) -> list[str]:
    """Download all output images and videos from a ComfyUI history entry."""
    downloaded = []
    for node_id, node_output in history_entry.get("outputs", {}).items():
        # Collect both images and videos/gifs
        items = []
        items.extend(node_output.get("images", []))
        items.extend(node_output.get("videos", []))
        items.extend(node_output.get("gifs", []))

        for item in items:
            filename = item.get("filename", "")
            subfolder = item.get("subfolder", "")
            item_type = item.get("type", "output")
            if not filename:
                continue

            url = (
                f"{COMFYUI_URL}/view?"
                f"filename={urllib.request.quote(filename)}"
                f"&subfolder={urllib.request.quote(subfolder)}"
                f"&type={urllib.request.quote(item_type)}"
            )
            try:
                ext = Path(filename).suffix or ".png"
                tmp = tempfile.NamedTemporaryFile(
                    delete=False, suffix=ext, prefix="comfyui_"
                )
                with urllib.request.urlopen(url, timeout=30) as resp:
                    tmp.write(resp.read())
                tmp.close()
                downloaded.append(tmp.name)
                log.info(f"Downloaded: {filename} -> {tmp.name}")
            except Exception as e:
                log.warning(f"Failed to download {filename}: {e}")

    return downloaded


# ============================================================================
# MAIN ENTRY POINT
# ============================================================================

def run(
    prompt: str,
    model: str = "",
    negative: str = "",
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    steps: int = DEFAULT_STEPS,
    amount: int = 1,
    reference_image_path: str = "",
    progress_callback=None,
    output_dir: str = "",
) -> dict:
    """
    Generate images/videos with ComfyUI.

    Smart dispatch:
      - Auto-selects Flux (img2img) vs Z_Turbo (txt2img) based on reference image.
      - Detects background-remove intent from prompt keywords.
      - Auto-toggles landscape/portrait based on prompt.

    If output_dir is set, copies results there (for builder integration).

    Returns dict: {success, image_paths, message, error}
    """
    log.info(f"ComfyUI run: model={model}, prompt={prompt[:60]}...")

    # Smart dispatch: auto-select workflow if no model specified
    model = detect_intent(prompt, model, reference_image_path)
    log.info(f"Workflow selected: {model}")

    # Auto-resolution: landscape/portrait toggle
    width, height = auto_resolution(width, height, prompt)
    log.info(f"Resolution: {width}x{height}")

    # Ensure ComfyUI is running (auto-start if needed)
    if not _ensure_comfyui_running(timeout=120):
        return {
            "success": False,
            "image_paths": [],
            "message": "",
            "error": (
                f"ComfyUI not reachable at {COMFYUI_URL} and could not be started. "
                f"Check COMFYUI_PATH={COMFYUI_PATH}"
            ),
        }

    # Discover workflows
    workflows = discover_workflows()
    if not workflows:
        return {
            "success": False,
            "image_paths": [],
            "message": "",
            "error": (
                "No workflow files found in skills/comfyui/workflows/. "
                "Save your workflow from ComfyUI web UI and place the JSON file there."
            ),
        }

    # Select workflow
    if model and model in workflows:
        workflow = workflows[model]
    elif model:
        # Fuzzy match
        for name in workflows:
            if model.lower() in name.lower() or name.lower() in model.lower():
                workflow = workflows[name]
                model = name
                break
        else:
            available = ", ".join(sorted(workflows.keys()))
            return {
                "success": False,
                "image_paths": [],
                "message": "",
                "error": f"Model '{model}' not found. Available: {available}",
            }
    else:
        model = next(iter(workflows))
        workflow = workflows[model]
        log.info(f"No model specified, using: {model}")

    # Upload reference image if provided
    ref_image_name = None
    if reference_image_path and os.path.isfile(reference_image_path):
        log.info(f"Uploading reference image: {reference_image_path}")
        ref_image_name = _upload_image(reference_image_path)
        if not ref_image_name:
            log.warning("Reference image upload failed, continuing without it")

    # Inject parameters
    wf = _inject_params(
        workflow,
        prompt=prompt,
        negative=negative or "nsfw, blurry, bad quality, watermark, text, deformed",
        width=width,
        height=height,
        steps=steps,
        seed=-1,
        batch_size=amount,
    )

    # Inject reference image
    if ref_image_name:
        wf = _inject_reference_image(wf, ref_image_name)

    # Queue and wait
    try:
        prompt_id = _queue_prompt(wf)
        log.info(f"Queued: {prompt_id} (model={model})")
    except Exception as e:
        return {
            "success": False,
            "image_paths": [],
            "message": "",
            "error": f"Failed to queue prompt: {e}",
        }

    try:
        history = _wait_for_result(prompt_id, timeout=1800, progress_callback=progress_callback)
    except TimeoutError as e:
        return {
            "success": False,
            "image_paths": [],
            "message": "",
            "error": str(e),
        }
    except Exception as e:
        return {
            "success": False,
            "image_paths": [],
            "message": "",
            "error": f"Error waiting for result: {e}",
        }

    # Download outputs (images + videos)
    output_paths = _download_outputs(history)

    if not output_paths:
        return {
            "success": False,
            "image_paths": [],
            "message": "",
            "error": "Generation completed but no output files found",
        }

    # If output_dir specified (builder integration), copy files there
    if output_dir and os.path.isdir(output_dir):
        copied_paths = []
        for src in output_paths:
            ext = Path(src).suffix or ".png"
            dest_name = f"generated_{len(copied_paths) + 1}{ext}"
            dest = os.path.join(output_dir, dest_name)
            try:
                import shutil as _shutil
                _shutil.copy2(src, dest)
                copied_paths.append(dest)
                log.info(f"Copied to project: {dest}")
            except Exception as e:
                log.warning(f"Failed to copy to {dest}: {e}")
        if copied_paths:
            output_paths.extend(copied_paths)

    return {
        "success": True,
        "image_paths": output_paths,
        "message": (
            f"Generated {len(output_paths)} file(s) "
            f"with {model} ({width}x{height}, {steps} steps)"
        ),
        "error": "",
    }
