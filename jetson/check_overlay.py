#!/usr/bin/env python3
"""Check jetson/overlay/path-cameras.dts against the vendors' own overlays, without booting it.

A wrong overlay shows up only as a camera that does not probe, so this compares, for each wiring:
  * every property of each camera's node, modes and endpoint with the vendor's node for that connector;
    the only differences allowed are the ones listed in ALLOWED below (and in the header of the .dts);
  * the sensor -> nvcsi -> vi endpoint chain of each camera: it closes, and lanes, port index and
    interface name agree along it;
  * the labels the bootloader has to resolve: all present in the base device tree.

Run by jetson/build.sh:  check_overlay.py <overlay.dtbo> <vc|tevs>=<vendor.dtbo> ... [--base <base.dtb>]
"""
import os
import re
import subprocess
import sys
from pathlib import Path

ALLOWED = {  # (camera, node relative to the camera node, property): (vendor value, our value)
    ("vc", "mode0", "embedded_metadata_height"): ('"1"', f'"{os.environ.get("IMX412_METADATA_H", "2")}"'),
    **{("tevs", f"mode{n}", prop): values for n in range(6) for prop, values in {
        "mode_type": ('"yuv"', '"bayer"'), "pixel_phase": ('"uyvy"', '"grbg"'), "csi_pixel_bit_depth": ('"16"', '"10"'),
        "pix_clk_hz": None}.items()},  # None: checked numerically below (x 16/10)
}
SENSOR = {"vc": "vc_mipi@1a", "tevs": "rpi22_tevs_"}  # Node name (prefix) of each camera under cam_i2cmux/i2c@N
PHANDLE_PROPS = {"remote-endpoint", "standby-gpios"}  # Start with a phandle local to the file: followed below instead of compared as text
IGNORED = {"phandle"}  # Only says that a label points at the node
PORT_OF = {'"serial_b"': 1, '"serial_c"': 2}  # tegra_sinterface -> CSI port index on this carrier


def load(dtb):
    """Decompile a .dtb/.dtbo and return {node path: {property: value text}} plus {phandle: node path}."""
    text = subprocess.run(["dtc", "-I", "dtb", "-O", "dts", "-q", str(dtb)], capture_output=True, text=True, check=True).stdout
    nodes, path = {"/": {}}, []
    for line in text.splitlines():
        line = line.strip()
        if line.endswith("{"):
            path.append(line[:-1].strip())
            nodes["/" + "/".join(path[1:])] = {}
        elif line == "};":
            path.pop()
        elif path and line.endswith(";"):
            name, _, value = line[:-1].partition(" = ")
            nodes["/" + "/".join(path[1:])][name] = value
    phandles = {props["phandle"]: node for node, props in nodes.items() if "phandle" in props}
    return nodes, phandles


def camera_nodes(nodes, name):
    """{mux leg: [paths of the camera node]} for one camera type. TechNexion spreads a node over two fragments."""
    found = {}
    for path in nodes:
        m = re.search(r"/cam_i2cmux/i2c@(\d)/" + re.escape(SENSOR[name]) + r"[^/]*$", path)
        if m and not path.startswith("/__"):  # Not dtc's __local_fixups__ mirror of the tree
            found.setdefault(int(m[1]), []).append(path)
    return found


def subtree(nodes, roots):
    """{node relative to the camera node: properties}, merged over the fragments that contribute to it."""
    merged = {}
    for root in roots:
        for path, props in nodes.items():
            if path == root or path.startswith(root + "/"):
                merged.setdefault(path[len(root):].lstrip("/"), {}).update(props)
    return merged


def follow(nodes, phandles, path, prop="remote-endpoint"):
    target = phandles.get(nodes[path][prop])
    if target is None:
        fail(f"{path}: {prop} points nowhere")
    return target


def fail(message):
    sys.exit(f"check_overlay: FAILED: {message}")


def main():
    args = sys.argv[1:]
    base = args[args.index("--base") + 1] if "--base" in args else None
    ours, our_phandles = load(args[0])
    vendors = dict(arg.split("=", 1) for arg in args[1:] if "=" in arg)
    checked = 0
    for name, vendor_dtb in vendors.items():
        theirs, _ = load(vendor_dtb)
        mine, vendor_cams = camera_nodes(ours, name), camera_nodes(theirs, name)
        if len(mine) != 1:
            fail(f"expected exactly one {name} camera in the overlay, found {sorted(mine)}")
        (leg, (root,)), = mine.items()
        if leg not in vendor_cams:
            fail(f"{vendor_dtb} has no {name} camera on mux leg {leg} to compare with")
        a, b = subtree(ours, [root]), subtree(theirs, vendor_cams[leg])
        if set(a) != set(b):
            fail(f"{name}: child nodes differ from the vendor's: {sorted(set(a) ^ set(b))}")
        for node in a:
            for prop in sorted((set(a[node]) | set(b[node])) - IGNORED):
                if prop in PHANDLE_PROPS:
                    if (prop in a[node]) != (prop in b[node]):
                        fail(f"{name}: {node or '.'}/{prop} is present in only one of the two")
                    continue
                va, vb, allowed = a[node].get(prop), b[node].get(prop), ALLOWED.get((name, node, prop), ())
                if (name, node, prop) in ALLOWED and allowed is None:  # pix_clk_hz: same lane rate at 10 bits as at 16
                    if int(va.strip('"')) * 10 != int(vb.strip('"')) * 16:
                        fail(f"{name}: {node}/pix_clk_hz {va} does not keep the vendor's lane rate ({vb} at 16 bit)")
                elif va != vb and (vb, va) != allowed:
                    fail(f"{name}: {node or '.'}/{prop} is {va}, the vendor has {vb}")
                checked += 1
        if name == "tevs":  # Its standby line: pin 6 of the adaptor's expander on the same mux leg, as the vendor has it
            cells, vendor_cells = a[""]["standby-gpios"].strip("<>").split(), b[""]["standby-gpios"].strip("<>").split()
            expander = our_phandles.get(f"<{cells[0]}>", "")
            if cells[1:] != vendor_cells[1:] or not re.search(rf"/cam_i2cmux/i2c@{leg}/pca9554_\w@27$", expander):
                fail(f"tevs: standby-gpios is <{' '.join(cells)}> -> {expander or 'nothing'}, expected pin {vendor_cells[1]} of the expander on leg {leg}")
            vendor_expander = next(props for path, props in theirs.items() if re.search(rf"/cam_i2cmux/i2c@{leg}/pca9554_\w@27$", path))
            for prop in sorted((set(ours[expander]) | set(vendor_expander)) - IGNORED - {"vcc-supply"}):
                if ours[expander].get(prop) != vendor_expander.get(prop):
                    fail(f"tevs: expander {prop} is {ours[expander].get(prop)}, the vendor has {vendor_expander.get(prop)}")
                checked += 1
        # The chain sensor -> nvcsi channel -> vi port, and back.
        sensor_ep = root + "/ports/port@0/endpoint"
        csi_in = follow(ours, our_phandles, sensor_ep)
        csi_out = re.sub(r"port@0/endpoint@\d$", "", csi_in) + "port@1/" + next(
            p.rsplit("/", 1)[1] for p in ours if p.startswith(re.sub(r"port@0/endpoint@\d$", "port@1/", csi_in)))
        vi_in = follow(ours, our_phandles, csi_out)
        if follow(ours, our_phandles, csi_in) != sensor_ep or follow(ours, our_phandles, vi_in) != csi_out:
            fail(f"{name}: the sensor/nvcsi/vi endpoints do not point back at each other")
        if f"/channel@{leg}/" not in csi_in or not vi_in.endswith(f"/tegra-capture-vi/ports/port@{leg}/endpoint"):
            fail(f"{name}: on mux leg {leg} but wired to {csi_in} and {vi_in}")
        mode0 = ours[root + "/mode0"]
        lanes, port = int(mode0["num_lanes"].strip('"')), PORT_OF[mode0["tegra_sinterface"]]
        for endpoint in (sensor_ep, csi_in, vi_in):
            if int(ours[endpoint]["bus-width"].strip("<>"), 16) != lanes or int(ours[endpoint]["port-index"].strip("<>"), 16) != port:
                fail(f"{name}: {endpoint} disagrees with mode0 ({lanes} lanes on {mode0['tegra_sinterface']})")
        if port != leg + 1 or (leg == 0) != (mode0.get("lane_polarity") == '"6"') or (leg == 0 and lanes != 2):
            fail(f"{name}: mux leg {leg} needs CSI port {leg + 1}" + (", 2 lanes and lane_polarity 6" if leg == 0 else " and no lane swap"))
        print(f"check_overlay: {name}: matches the vendor's node on CAM{leg} ({lanes} lanes, {mode0['tegra_sinterface'].strip(chr(34))})")
    # Labels resolved at boot.
    fixups = sorted(ours.get("/__fixups__", {}))
    if base:
        known = load(base)[0].get("/__symbols__", {})
        if missing := [label for label in fixups if label not in known]:
            fail(f"{base} has no label(s) {missing}: the bootloader would refuse the overlay")
    cameras = [paths[0] for n in ("vc", "tevs") for paths in camera_nodes(ours, n).values()]
    total, lanes_total = len(cameras), sum(int(ours[p + "/mode0"]["num_lanes"].strip('"')) for p in cameras)
    platform = next(p for p in ours if p.endswith("/tegra-camera-platform") and not p.startswith("/__"))
    if int(ours[platform]["num_csi_lanes"].strip("<>"), 16) != lanes_total:
        fail(f"tegra-camera-platform num_csi_lanes is not the {lanes_total} lanes in use")
    print(f"check_overlay: OK: {checked} properties compared for {total} cameras; base labels needed: {', '.join(fixups)}"
          + (f" (all in {Path(base).name})" if base else ""))


if __name__ == "__main__":
    main()
