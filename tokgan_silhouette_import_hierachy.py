import json
from fx import *
from gui import undo
from PySide2.QtWidgets import QFileDialog
from PySide2.QtCore import QSettings
from PySide2.QtCore import QDir
from PySide2.QtCore import QFileInfo
from tools.window import get_main_window
import json


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


def import_json_to_silhouette(path, use_bspline=True):
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
    main_loop(objects, root_layer, use_bspline)


def main_loop(objects, root_layer, use_bspline=True):
    TOTAL: int = len(objects.keys())
    for count, (obj_name, obj) in enumerate(objects.items(), start=1):
        undo.run(inner_loop, "Per_animated Shape", obj_name, obj, use_bspline)
        print(
            ".",
        )
        print(f"{float(count/TOTAL)*100.0:02.1f}%")


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
    MIN_FRAME: int = 1
    MAX_FRAME: int = 10001
    # --- Improved Visibility Logic ---
    # Instead of just first/last frame, we map all visibility changes
    all_frames = sorted([int(f) for f in frames.keys()])
    vis_prop = shape.property("opacity")
    vis_prop.value = 0
    vis_prop.constant = False
    layer_editor = PropertyEditor(vis_prop)
    layer_editor.setValue(0, MIN_FRAME)
    if all_frames:
        # Hide shape by default outside of data range
        layer_editor.setValue(0, all_frames[0] - 1)

        # Set explicit keyframes for visibility from the JSON
        for frame_str, vis_value in visibility_data.items():
            frame = int(frame_str)
            layer_editor.setValue(100 if bool(vis_value) else 0, frame)
            layer_editor.setValue(0, frame + 1)

        layer_editor.setValue(0, all_frames[-1] + 1)
    # layer_editor.setValue(0, MAX_FRAME )
    layer_editor.execute()


def inner_loop(obj_name, obj, use_bspline=True):
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
    for frame_str, frame_data in frames.items():
        transformed_points = []
        frame = int(frame_str)

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
        if path:
            save_tokgan_dir(QFileInfo(path).dir())
            undo.run(import_json_to_silhouette, "Import notes", path)


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
        if path:
            save_tokgan_dir(QFileInfo(path).dir())
            undo.run(import_json_to_silhouette, "Import notes", path)


if __name__ == "__main__":
    CreateImportTokganAction().execute()
else:
    addAction(ImportTokganAction())
