import json
from fx import *
from gui import undo
from PySide2.QtWidgets import QFileDialog, QInputDialog
from PySide2.QtCore import QSettings
from PySide2.QtCore import QDir
from PySide2.QtCore import QFileInfo
from tools.window import get_main_window
import json
import datetime


FINGER_NAMES = ("A_thumb", "B_index", "C_middle", "D_ring", "E_pinky")
FINGER_ORDER = ["A_thumb", "B_index", "C_middle", "D_ring", "E_pinky"]

# ------------------------------------------------------------
# Utilities
# ------------------------------------------------------------


def get_or_create_layer(parent, name):
    """
    Silhouette equivalent:
    parent can be a RotoNode or a Layer object.
    """
    obj_prop = parent.property("objects")
    children = obj_prop.value
    for item in children:
        if isinstance(item, Layer) and item.label == name:
            return item
    layer = Layer()
    layer.label = name
    parent.objects.addObjects([layer])
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
    """
    Priority:
    1. 'resolution' field in JSON
    """
    # Check JSON data for resolution [New Improvement]
    json_res = data.get("resolution")
    if json_res and hasattr(json_res, "__getitem__") and len(json_res) == 2:
        return int(json_res[1])
    height = data.get("height")
    if height:
        return int(height)
    raise ValueError("Resolution Uknown")


def split_hand_part(part_name):
    for finger in FINGER_ORDER:
        if part_name.startswith(finger):
            return "fingers", finger
    return None, None


# ------------------------------------------------------------
# Main Importer
# ------------------------------------------------------------


def update_silhouette(path, shape, frame, transformed_points):
    path.points = transformed_points
    path_prop = shape.property("path")
    path_editor = PropertyEditor(path_prop)
    path_editor.setValue(path, frame)
    path_editor.execute()
    vis_prop = shape.property("opacity")
    vis_prop.constant = False


def import_json_to_silhouette(path, undersample=1, use_bspline=True):
    if not path:
        return

    with open(path, "r") as f:
        data = json.load(f)

    # Vertical resolution for Y-flip
    H = get_vertical_resolution(data)

    # Create Roto node
    x_offset = 1.0
    y_offset = -1.0
    objects = data["objects"]
    root_layer = activeNode()
    try:
        assert root_layer.isType("RotoNode")
    except AssertionError:
        print("Select a RotoNode")
        return
    main_loop(
        objects, root_layer, undersample=undersample, use_bspline=use_bspline
    )


def formatted_duration(duration: type(datetime.timedelta)):
    total_seconds = duration.total_seconds()
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    formatted_duration = "{:02.0f}:{:02.0f}:{:02.0f}".format(
        hours, minutes, seconds
    )
    milliseconds = duration.microseconds // 1000
    return f"{formatted_duration}.{milliseconds:03.0f}"


def main_loop(objects, root_layer, undersample=1, use_bspline=True):
    start = datetime.datetime.now()
    print(f"Start {start}")
    TOTAL: int = len(objects.keys())
    for count, (obj_name, obj) in enumerate(objects.items(), start=1):
        undo.run(
            inner_loop,
            "Per_animated Shape",
            obj_name,
            obj,
            use_bspline,
            undersample,
        )
        print(
            f"PROGRESS {float(count/TOTAL)*100.0:02.1f}% {count:05d} of {TOTAL:05d}"
        )
    end = datetime.datetime.now()

    elapsed = end - start
    print(f"Complete! took {formatted_duration(elapsed)} for {TOTAL} shapes")


def make_part_layer(obj_name, root_layer):
    person, region, side, part = parse_object_name(obj_name)
    side_layer_name = f"{person}_{region}_{side}"
    # Build hierarchy
    person_layer = get_or_create_layer(root_layer, person)
    region_layer = get_or_create_layer(person_layer, f"{person}_{region}")
    side_layer = get_or_create_layer(region_layer, side_layer_name)
    if region == "hand":
        group, finger = split_hand_part(part)
        if group == "fingers":
            fingers_layer = get_or_create_layer(
                side_layer, f"{person}_fingers_{side}"
            )
            finger_layer = get_or_create_layer(
                fingers_layer, f"{person}_{finger}_{side}"
            )
            part_layer = get_or_create_layer(finger_layer, f"{person}_{part}")
        else:
            part_layer = get_or_create_layer(side_layer, f"{person}_{part}")
    else:
        part_layer = get_or_create_layer(side_layer, f"{person}_{part}")
    return part_layer, part, side_layer_name


def key_enabled_layer(shape, frames, visibility_data):
    if not visibility_data:
        return
    MIN_FRAME: int = 1
    vis_prop = shape.property("opacity")
    vis_prop.constant = False
    layer_editor = PropertyEditor(vis_prop)
    # Initial state: Hidden
    layer_editor.setValue(0, MIN_FRAME)
    # Sort frames to handle them chronologically
    sorted_frames = sorted([int(f) for f in visibility_data.keys()])

    # Initial state: Hidden
    layer_editor.setValue(0, sorted_frames[0] - 1)

    last_vis = None

    for (i, frame) in enumerate(sorted_frames):
        current_vis = bool(visibility_data[str(frame)])

        # Only set a key if the visibility state has changed
        if current_vis != last_vis:
            val = 100 if current_vis else 0

            # If turning OFF, we want it to stay ON until this frame,
            # then go OFF. If turning ON, we want it OFF until now.
            layer_editor.setValue(val, frame)

            last_vis = current_vis
        if i + 1 < len(sorted_frames) and sorted_frames[i+1] - frame > 2:
            layer_editor.setValue(0, frame + 1)
            layer_editor.setValue(0, sorted_frames[i+1]-1)
            last_vis = 0
    # Ensure it turns off after the last known frame
    layer_editor.setValue(0, sorted_frames[-1] + 1)
    layer_editor.execute()


def filter_frames(all_frames, nth):
    if nth <= 1 or len(all_frames) <= 2:
        return all_frames

    # Get every Nth frame
    sampled = all_frames[::nth]

    # Ensure the last frame is included if the stride skipped it
    if all_frames[-1] not in sampled:
        sampled.append(all_frames[-1])

    return sampled


def inner_loop(obj_name, obj, use_bspline=True, undersample=1):
    root_layer = activeNode()
    session = activeSession()
    matrix = session.imageToWorldTransform
    shape_type = "bspline" if use_bspline else "bezier"
    frames = obj.get("frames", {})
    visibility = obj.get("visibility", {})
    part_layer, part, side_layer_name = make_part_layer(obj_name, root_layer)
    if not frames:
        return False

    shape = Shape(Shape.Bspline)
    shape.label = f"{side_layer_name}_{part}Shape".replace(":", "_").replace(
        ".", "_"
    )
    visibility_data = obj.get("visibility", {})

    first_frame = True
    all_frames = [int(i) for i in frames.keys()]
    filtered_frames = filter_frames(all_frames, undersample)

    for frame_str, frame_data in frames.items():
        transformed_points = []
        frame = int(frame_str)
        if frame not in filtered_frames:
            continue

        pts = frame_data["points"]
        if first_frame:
            path = shape.createPath(frame)
            path.closed = True
            first_frame = False

        for p in pts:
            x = p["x"]
            y = p["y"]
            world_pt = matrix * Point(x, y)
            transformed_points.append((world_pt, 1, 1.0))

        # add the shape(s) to the node's "objects" property
        update_silhouette(
            path=path,
            shape=shape,
            frame=frame,
            transformed_points=transformed_points,
        )
        first_frame = False
    key_enabled_layer(shape, frames, visibility_data)
    part_layer.objects.addObjects([shape])
    # select the new shape(s)
    select([root_layer])
    return True


def get_tokgan_dir():
    settings = QSettings()
    value = settings.value("directory.tokgan")
    if value:
        return QDir(value).path()
    return QDir.homePath()


def save_tokgan_dir(path):
    settings = QSettings()
    settings.setValue("directory.tokgan", path)


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
            import_json_to_silhouette(
                path, use_bspline=True, undersample=stride
            )


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
            import_json_to_silhouette(
                path, use_bspline=True, undersample=stride
            )


if __name__ == "__main__":
    CreateImportTokganAction().execute()
else:
    addAction(ImportTokganAction())
