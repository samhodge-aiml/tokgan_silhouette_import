import json
from fx import *
from PySide2.QtWidgets import QFileDialog, QInputDialog
from PySide2.QtCore import QSettings
from PySide2.QtCore import QDir
from PySide2.QtCore import QFileInfo
from tools.window import get_main_window
import datetime


FINGER_NAMES = ("A_thumb", "B_index", "C_middle", "D_ring", "E_pinky")
FINGER_ORDER = ["A_thumb", "B_index", "C_middle", "D_ring", "E_pinky"]

# ------------------------------------------------------------
# Utilities
# ------------------------------------------------------------

# OPT: Layer cache avoids repeated linear scans of children on every object.
# Before: get_or_create_layer() called parent.property("objects").value and
#         iterated all children every single call — O(n) per lookup, called
#         up to 5× per shape × 63 shapes = 315 redundant scans.
# After:  first hit populates _layer_cache[parent_id][name]; subsequent
#         lookups for the same (parent, name) pair are O(1) dict reads.
_layer_cache: dict = {}

def get_or_create_layer(parent, name):
    """
    Silhouette equivalent: parent can be a RotoNode or a Layer object.
    Uses a two-level dict cache keyed by (id(parent), name) so repeated
    calls for the same layer skip the children scan entirely.
    """
    parent_id = id(parent)
    if parent_id not in _layer_cache:
        _layer_cache[parent_id] = {}
    cache = _layer_cache[parent_id]
    if name in cache:
        return cache[name]

    # Not cached yet — do the scan once, then store result
    obj_prop = parent.property("objects")
    children = obj_prop.value
    for item in children:
        if isinstance(item, Layer) and item.label == name:
            cache[name] = item
            return item

    layer = Layer()
    layer.label = name
    parent.objects.addObjects([layer])
    cache[name] = layer
    return layer


def parse_object_name(obj_name):
    """
    Expected formats:
      person:region:side:part
    Pads missing fields with 'unknown'
    """
    parts = obj_name.split(":")
    while len(parts) < 4:
        parts.append("unknown")
    return parts[:4]


def get_vertical_resolution(data):
    json_res = data.get("resolution")
    if json_res and hasattr(json_res, "__getitem__") and len(json_res) == 2:
        return int(json_res[1])
    height = data.get("height")
    if height:
        return int(height)
    raise ValueError("Resolution Unknown")


def split_hand_part(part_name):
    for finger in FINGER_ORDER:
        if part_name.startswith(finger):
            return "fingers", finger
    return None, None


# ------------------------------------------------------------
# Main Importer
# ------------------------------------------------------------

# Original — fresh PropertyEditor per frame, proven to animate correctly.
def update_silhouette(path, shape, frame, transformed_points):
    path.points = transformed_points
    path_prop = shape.property("path")
    path_editor = PropertyEditor(path_prop)
    path_editor.setValue(path, frame)
    path_editor.execute()


def import_json_to_silhouette(path, undersample=1, use_bspline=True):
    if not path:
        return

    with open(path, "r") as f:
        data = json.load(f)

    objects = data["objects"]
    root_layer = activeNode()
    try:
        assert root_layer.isType("RotoNode")
    except AssertionError:
        print("Select a RotoNode")
        return

    # OPT: Reset layer cache for each import so stale ids don't accumulate
    # across multiple imports in the same session.
    _layer_cache.clear()

    main_loop(objects, root_layer, undersample=undersample, use_bspline=use_bspline)


def formatted_duration(duration: type(datetime.timedelta)):
    total_seconds = duration.total_seconds()
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    formatted_str = "{:02.0f}:{:02.0f}:{:02.0f}".format(hours, minutes, seconds)
    milliseconds = duration.microseconds // 1000
    return f"{formatted_str}.{milliseconds:03.0f}"


def main_loop(objects, root_layer, undersample=1, use_bspline=True):
    start = datetime.datetime.now()
    print(f"Start {start}")
    TOTAL: int = len(objects)
    beginUndo("Tokgan Import")

    # OPT: Fetch the transform matrix ONCE here and pass it down.
    # Before: activeSession() + session.imageToWorldTransform called inside
    #         inner_loop — meaning 63 redundant API lookups for the same matrix.
    session = activeSession()
    matrix = session.imageToWorldTransform

    # OPT: Pre-decompose matrix into scalar components for the hot point loop.
    # matrix * Point(x, y) goes through Silhouette's C++ operator every call.
    # Extracting the 2×2+translation lets us do the same math in pure Python
    # with no per-point object construction overhead.
    # Standard 3×3 homogeneous layout: [a,b,tx, c,d,ty, 0,0,1]
    # Silhouette Matrix elements are row-major: m[row][col]
    try:
        # 4×4 row-major matrix. For a 2D image-to-world transform the relevant
        # components live in the top-left 2×2 plus the translation column:
        #   [ a   b   _  tx ]   row 0
        #   [ c   d   _  ty ]   row 1
        #   [ _   _   _   _ ]   row 2
        #   [ _   _   _   _ ]   row 3
        a  = matrix.value(0, 0); b  = matrix.value(0, 1); tx = matrix.value(0, 3)
        c  = matrix.value(1, 0); d  = matrix.value(1, 1); ty = matrix.value(1, 3)
        USE_FAST_TRANSFORM = True
    except Exception:
        # Fallback: value() not supported — use original matrix * Point() call
        USE_FAST_TRANSFORM = False

    p = PreviewProgressHandler()
    p.begin()
    p.title = "Tokgan Import"
    p.total = TOTAL
    p.value = 0
    p_floatvalue = 0.0
    increment = 100.0 / TOTAL

    for count, (obj_name, obj) in enumerate(objects.items(), start=1):
        if p.canceled:
            TOTAL = count
            break

        inner_loop(
            obj_name,
            obj,
            use_bspline,
            undersample,
            root_layer,
            matrix,
            a, b, tx, c, d, ty,
            USE_FAST_TRANSFORM,
        )
        p_floatvalue += increment
        p.value = int(p_floatvalue)


    end = datetime.datetime.now()
    p.end()
    endUndo()

    elapsed = end - start
    print(f"Complete! took {formatted_duration(elapsed)} for {TOTAL} shapes")


def make_part_layer(obj_name, root_layer):
    person, region, side, part = parse_object_name(obj_name)
    side_layer_name = f"{person}_{region}_{side}"
    person_layer  = get_or_create_layer(root_layer, person)
    region_layer  = get_or_create_layer(person_layer, f"{person}_{region}")
    side_layer    = get_or_create_layer(region_layer, side_layer_name)
    if region == "hand":
        group, finger = split_hand_part(part)
        if group == "fingers":
            fingers_layer = get_or_create_layer(side_layer, f"{person}_fingers_{side}")
            finger_layer  = get_or_create_layer(fingers_layer, f"{person}_{finger}_{side}")
            part_layer    = get_or_create_layer(finger_layer, f"{person}_{part}")
        else:
            part_layer = get_or_create_layer(side_layer, f"{person}_{part}")
    else:
        part_layer = get_or_create_layer(side_layer, f"{person}_{part}")
    return part_layer, part, side_layer_name


def key_enabled_layer(shape, frames, visibility_data):
    if not visibility_data:
        return
    vis_prop = shape.property("opacity")
    vis_prop.constant = False
    layer_editor = PropertyEditor(vis_prop)
    sorted_frames = sorted(int(f) for f in visibility_data.keys())

    layer_editor.setValue(0, sorted_frames[0] - 1)
    last_vis = None

    for i, frame in enumerate(sorted_frames):
        current_vis = bool(visibility_data[str(frame)])
        if current_vis != last_vis:
            layer_editor.setValue(100 if current_vis else 0, frame)
            last_vis = current_vis
        if i + 1 < len(sorted_frames) and sorted_frames[i + 1] - frame > 2:
            layer_editor.setValue(0, frame + 1)
            layer_editor.setValue(0, sorted_frames[i + 1] - 1)
            last_vis = 0

    layer_editor.setValue(0, sorted_frames[-1] + 1)
    layer_editor.execute()


def filter_frames(all_frames, nth):
    if nth <= 1 or len(all_frames) <= 2:
        return all_frames
    sampled = all_frames[::nth]
    if all_frames[-1] not in sampled:
        sampled.append(all_frames[-1])
    return sampled


def inner_loop(
    obj_name, obj, use_bspline, undersample,
    root_layer, matrix,
    a, b, tx, c, d, ty,
    USE_FAST_TRANSFORM,
):
    # OPT: root_layer and matrix are passed in — no activeNode() /
    #      activeSession() call per shape anymore.

    frames = obj.get("frames", {})
    if not frames:
        return False

    visibility_data = obj.get("visibility", {})
    part_layer, part, side_layer_name = make_part_layer(obj_name, root_layer)

    shape = Shape(Shape.Bspline)
    shape.label = (
        f"{side_layer_name}_{part}Shape".replace(":", "_").replace(".", "_")
    )

    all_frames   = [int(i)-1 for i in frames.keys()]
    filtered_set = set(filter_frames(all_frames, undersample))  # OPT: set for O(1) membership test

    first_frame = True
    path        = None

    for frame_str, frame_data in frames.items():
        frame = int(frame_str)-1
        if frame not in filtered_set:
            continue
        pts = frame_data["points"]

        if first_frame:
            path = shape.createPath(frame)
            path.closed = True
            first_frame = False

        # OPT: Point transform — pure Python math instead of matrix * Point()
        # C++ call per point. ~90k calls total across all shapes/frames.
        if USE_FAST_TRANSFORM:
            transformed_points = [
                (Point(a * p["x"] + b * p["y"] + tx,
                       c * p["x"] + d * p["y"] + ty), 1, 1.0)
                for p in pts
            ]
        else:
            transformed_points = [
                (matrix * Point(p["x"], p["y"]), 1, 1.0)
                for p in pts
            ]

        # Original signature — fresh PropertyEditor per frame, proven to work
        update_silhouette(path, shape, frame, transformed_points)


    vis_prop = shape.property("opacity")
    vis_prop.constant = False
    key_enabled_layer(shape, frames, visibility_data)

    part_layer.objects.addObjects([shape])
    select([root_layer])
    return True


# ------------------------------------------------------------
# Settings helpers
# ------------------------------------------------------------

def get_tokgan_dir():
    settings = QSettings()
    value = settings.value("directory.tokgan")
    if value:
        return QDir(value).path()
    return QDir.homePath()


def save_tokgan_dir(path):
    settings = QSettings()
    settings.setValue("directory.tokgan", path)


# ------------------------------------------------------------
# Actions
# ------------------------------------------------------------

class ImportTokganAction(Action):
    def __init__(self):
        Action.__init__(self, "Tokgan|Import...")

    def available(self):
        assert activeNode().isType("RotoNode"), "Active Node must be Roto"

    def execute(self):
        main_window = get_main_window()
        dir = get_tokgan_dir()
        path, _ = QFileDialog.getOpenFileName(
            main_window, "Choose Tokgan JSON file", dir, "JSON Files (*.json)"
        )
        stride, ok = QInputDialog.getInt(
            None, "Import Settings", "Undersample Rate:", 1, 1, 100, 1
        )
        if path and ok:
            print(f"Loading {path} with undersample value {stride}")
            save_tokgan_dir(QFileInfo(path).dir())
            import_json_to_silhouette(path, use_bspline=True, undersample=stride)


class CreateImportTokganAction(Action):
    def __init__(self):
        Action.__init__(self, "Tokgan|Import Roto")

    def available(self):
        assert activeNode().isType("RotoNode"), "Active Node must be Roto"

    def execute(self):
        assert activeNode().isType("RotoNode"), "Active Node must be Roto"
        assert activeSession(), "Active Session Required for Matrix"
        main_window = get_main_window()
        dir = get_tokgan_dir()
        path, _ = QFileDialog.getOpenFileName(
            main_window, "Choose Tokgan JSON file", dir, "JSON Files (*.json)"
        )
        stride, ok = QInputDialog.getInt(
            None, "Import Settings", "Undersample Rate:", 1, 1, 100, 1
        )
        if path and ok:
            print(f"Loading {path} with undersample value {stride}")
            save_tokgan_dir(QFileInfo(path).dir())
            import_json_to_silhouette(path, use_bspline=True, undersample=stride)


if __name__ == "__main__":
    CreateImportTokganAction().execute()
else:
    addAction(ImportTokganAction())