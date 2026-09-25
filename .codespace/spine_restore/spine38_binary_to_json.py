#!/usr/bin/env python3
"""
Spine 3.8 binary (.skel) → JSON converter
Based on official EsotericSoftware spine-runtimes 3.8 SkeletonBinary (C# / Java).
"""

from __future__ import annotations

import json
import struct
import sys
from pathlib import Path
from typing import Any, Optional


class Spine38BinaryReader:
    # AttachmentType enum order from spine-runtimes 3.8
    ATTACHMENT_REGION = 0
    ATTACHMENT_BOUNDINGBOX = 1
    ATTACHMENT_MESH = 2
    ATTACHMENT_LINKEDMESH = 3
    ATTACHMENT_PATH = 4
    ATTACHMENT_POINT = 5
    ATTACHMENT_CLIPPING = 6

    TRANSFORM_MODES = [
        "normal",
        "onlyTranslation",
        "noRotationOrReflection",
        "noScale",
        "noScaleOrReflection",
    ]

    BLEND_MODES = ["normal", "additive", "multiply", "screen"]

    def __init__(self, data: bytes, scale: float = 1.0):
        self.data = data
        self.pos = 0
        self.scale = scale
        self.strings: list[str] = []

    # ---- low-level ----
    def _need(self, n: int) -> None:
        if self.pos + n > len(self.data):
            raise EOFError(f"Need {n} bytes at pos {self.pos}, file size {len(self.data)}")

    def read_byte(self) -> int:
        self._need(1)
        b = self.data[self.pos]
        self.pos += 1
        return b

    def read_sbyte(self) -> int:
        b = self.read_byte()
        return b - 256 if b > 127 else b

    def read_boolean(self) -> bool:
        return self.read_byte() != 0

    def read_int(self) -> int:
        """Big-endian signed 32-bit."""
        self._need(4)
        v = struct.unpack_from(">i", self.data, self.pos)[0]
        self.pos += 4
        return v

    def read_int_var(self, optimize_positive: bool = True) -> int:
        b = self.read_byte()
        result = b & 0x7F
        if b & 0x80:
            b = self.read_byte()
            result |= (b & 0x7F) << 7
            if b & 0x80:
                b = self.read_byte()
                result |= (b & 0x7F) << 14
                if b & 0x80:
                    b = self.read_byte()
                    result |= (b & 0x7F) << 21
                    if b & 0x80:
                        result |= (self.read_byte() & 0x7F) << 28
        if not optimize_positive:
            result = (result >> 1) ^ -(result & 1)
        return result

    def read_float(self) -> float:
        """Big-endian float32."""
        self._need(4)
        v = struct.unpack_from(">f", self.data, self.pos)[0]
        self.pos += 4
        return v

    def read_string(self) -> Optional[str]:
        """
        Official format:
          byteCount = varint
          0 → null
          1 → ""
          else → read (byteCount - 1) UTF-8 bytes
        """
        byte_count = self.read_int_var(True)
        if byte_count == 0:
            return None
        if byte_count == 1:
            return ""
        byte_count -= 1
        self._need(byte_count)
        s = self.data[self.pos : self.pos + byte_count].decode("utf-8", errors="replace")
        self.pos += byte_count
        return s

    def read_string_ref(self) -> Optional[str]:
        index = self.read_int_var(True)
        if index == 0:
            return None
        return self.strings[index - 1]

    def read_color_rgba(self) -> str:
        """Returns #RRGGBBAA hex (Spine JSON style without #)."""
        c = self.read_int() & 0xFFFFFFFF
        return f"{c:08x}"

    # ---- high-level ----
    def read_skeleton_data(self) -> dict[str, Any]:
        skel: dict[str, Any] = {}

        # Header
        h = self.read_string()
        v = self.read_string()
        skel["hash"] = h
        skel["spine"] = v
        if v == "3.8.75":
            raise ValueError("Unsupported skeleton data 3.8.75")

        x = self.read_float()
        y = self.read_float()
        width = self.read_float()
        height = self.read_float()
        skel["x"] = x
        skel["y"] = y
        skel["width"] = width
        skel["height"] = height

        nonessential = self.read_boolean()
        if nonessential:
            skel["fps"] = self.read_float()
            img = self.read_string()
            aud = self.read_string()
            if img:
                skel["images"] = img
            if aud:
                skel["audio"] = aud

        # String pool
        n = self.read_int_var(True)
        self.strings = []
        for _ in range(n):
            self.strings.append(self.read_string() or "")

        # Bones
        bones: list[dict] = []
        n = self.read_int_var(True)
        for i in range(n):
            name = self.read_string() or f"bone{i}"
            parent_idx = self.read_int_var(True) if i > 0 else -1
            bone: dict[str, Any] = {"name": name}
            if parent_idx >= 0:
                bone["parent"] = bones[parent_idx]["name"]
            rot = self.read_float()
            bx = self.read_float() * self.scale
            by = self.read_float() * self.scale
            sx = self.read_float()
            sy = self.read_float()
            shx = self.read_float()
            shy = self.read_float()
            length = self.read_float() * self.scale
            transform_mode = self.read_int_var(True)
            skin_required = self.read_boolean()
            if rot:
                bone["rotation"] = rot
            if bx:
                bone["x"] = bx
            if by:
                bone["y"] = by
            if sx != 1:
                bone["scaleX"] = sx
            if sy != 1:
                bone["scaleY"] = sy
            if shx:
                bone["shearX"] = shx
            if shy:
                bone["shearY"] = shy
            if length:
                bone["length"] = length
            if transform_mode and transform_mode < len(self.TRANSFORM_MODES):
                tm = self.TRANSFORM_MODES[transform_mode]
                if tm != "normal":
                    bone["transform"] = tm
            if skin_required:
                bone["skin"] = True
            if nonessential:
                self.read_int()  # bone color, skip
            bones.append(bone)

        # slots
        slots: list[dict] = []
        n = self.read_int_var(True)
        for i in range(n):
            slot_name = self.read_string() or f"slot{i}"
            bone_idx = self.read_int_var(True)
            color = self.read_color_rgba()
            dark = self.read_int()  # 0x00rrggbb or -1
            attachment_name = self.read_string_ref()
            blend = self.read_int_var(True)
            slot: dict[str, Any] = {
                "name": slot_name,
                "bone": bones[bone_idx]["name"],
            }
            if color != "ffffffff":
                slot["color"] = color
            if dark != -1 and dark != 0xFFFFFFFF:
                # dark is 0x00rrggbb in some versions; store as hex
                slot["dark"] = f"{dark & 0xFFFFFF:06x}"
            if attachment_name:
                slot["attachment"] = attachment_name
            if blend and blend < len(self.BLEND_MODES) and self.BLEND_MODES[blend] != "normal":
                slot["blend"] = self.BLEND_MODES[blend]
            slots.append(slot)

        # IK
        iks: list[dict] = []
        n = self.read_int_var(True)
        for _ in range(n):
            name = self.read_string() or ""
            order = self.read_int_var(True)
            skin_req = self.read_boolean()
            nn = self.read_int_var(True)
            bone_names = [bones[self.read_int_var(True)]["name"] for _ in range(nn)]
            target = bones[self.read_int_var(True)]["name"]
            mix = self.read_float()
            softness = self.read_float() * self.scale
            bend = self.read_sbyte()
            compress = self.read_boolean()
            stretch = self.read_boolean()
            uniform = self.read_boolean()
            ik: dict[str, Any] = {
                "name": name,
                "order": order,
                "bones": bone_names,
                "target": target,
            }
            if skin_req:
                ik["skin"] = True
            if mix != 1:
                ik["mix"] = mix
            if softness:
                ik["softness"] = softness
            if bend != 1:
                ik["bendPositive"] = bend > 0
            if compress:
                ik["compress"] = True
            if stretch:
                ik["stretch"] = True
            if uniform:
                ik["uniform"] = True
            iks.append(ik)

        # Transform constraints
        transforms: list[dict] = []
        n = self.read_int_var(True)
        for _ in range(n):
            name = self.read_string() or ""
            order = self.read_int_var(True)
            skin_req = self.read_boolean()
            nn = self.read_int_var(True)
            bone_names = [bones[self.read_int_var(True)]["name"] for _ in range(nn)]
            target = bones[self.read_int_var(True)]["name"]
            local = self.read_boolean()
            relative = self.read_boolean()
            offset_rot = self.read_float()
            offset_x = self.read_float() * self.scale
            offset_y = self.read_float() * self.scale
            offset_sx = self.read_float()
            offset_sy = self.read_float()
            offset_shy = self.read_float()
            rotate_mix = self.read_float()
            translate_mix = self.read_float()
            scale_mix = self.read_float()
            shear_mix = self.read_float()
            t: dict[str, Any] = {
                "name": name,
                "order": order,
                "bones": bone_names,
                "target": target,
            }
            if skin_req:
                t["skin"] = True
            if local:
                t["local"] = True
            if relative:
                t["relative"] = True
            if offset_rot:
                t["rotation"] = offset_rot
            if offset_x:
                t["x"] = offset_x
            if offset_y:
                t["y"] = offset_y
            if offset_sx:
                t["scaleX"] = offset_sx
            if offset_sy:
                t["scaleY"] = offset_sy
            if offset_shy:
                t["shearY"] = offset_shy
            if rotate_mix != 1:
                t["rotateMix"] = rotate_mix
            if translate_mix != 1:
                t["translateMix"] = translate_mix
            if scale_mix != 1:
                t["scaleMix"] = scale_mix
            if shear_mix != 1:
                t["shearMix"] = shear_mix
            transforms.append(t)

        # Path constraints
        paths: list[dict] = []
        n = self.read_int_var(True)
        for _ in range(n):
            name = self.read_string() or ""
            order = self.read_int_var(True)
            skin_req = self.read_boolean()
            nn = self.read_int_var(True)
            bone_names = [bones[self.read_int_var(True)]["name"] for _ in range(nn)]
            target_slot = slots[self.read_int_var(True)]["name"]
            position_mode = self.read_int_var(True)  # 0 fixed, 1 percent
            spacing_mode = self.read_int_var(True)
            rotate_mode = self.read_int_var(True)
            offset_rot = self.read_float()
            position = self.read_float()
            if position_mode == 0:  # fixed
                position *= self.scale
            spacing = self.read_float()
            if spacing_mode in (0, 1):  # length or fixed
                spacing *= self.scale
            rotate_mix = self.read_float()
            translate_mix = self.read_float()
            p: dict[str, Any] = {
                "name": name,
                "order": order,
                "bones": bone_names,
                "target": target_slot,
            }
            if skin_req:
                p["skin"] = True
            # modes omitted if default
            if offset_rot:
                p["rotation"] = offset_rot
            if position:
                p["position"] = position
            if spacing:
                p["spacing"] = spacing
            if rotate_mix != 1:
                p["rotateMix"] = rotate_mix
            if translate_mix != 1:
                p["translateMix"] = translate_mix
            paths.append(p)

        # Skins
        skins_out: list[dict] = []
        default_skin = self._read_skin(True, nonessential, bones, slots)
        if default_skin:
            skins_out.append(default_skin)

        n = self.read_int_var(True)
        for _ in range(n):
            skin = self._read_skin(False, nonessential, bones, slots)
            if skin:
                skins_out.append(skin)

        # Events
        events: list[dict] = []
        n = self.read_int_var(True)
        for _ in range(n):
            name = self.read_string_ref() or ""
            ev: dict[str, Any] = {"name": name}
            ev["int"] = self.read_int_var(False)
            ev["float"] = self.read_float()
            s = self.read_string()
            if s is not None:
                ev["string"] = s
            audio = self.read_string()
            if audio:
                ev["audio"] = audio
                ev["volume"] = self.read_float()
                ev["balance"] = self.read_float()
            events.append(ev)

        # Animations
        animations: dict[str, Any] = {}
        n = self.read_int_var(True)
        for _ in range(n):
            anim_name = self.read_string() or "animation"
            animations[anim_name] = self._read_animation(bones, slots, iks, transforms, paths, events)

        # Build document
        doc: dict[str, Any] = {"skeleton": skel, "bones": bones, "slots": slots}
        if iks:
            doc["ik"] = iks
        if transforms:
            doc["transform"] = transforms
        if paths:
            doc["path"] = paths
        if skins_out:
            doc["skins"] = skins_out
        if events:
            doc["events"] = {e["name"]: {k: v for k, v in e.items() if k != "name"} for e in events}
        if animations:
            doc["animations"] = animations
        return doc

    def _read_skin(
        self, default_skin: bool, nonessential: bool, bones: list, slots: list
    ) -> Optional[dict]:
        if default_skin:
            slot_count = self.read_int_var(True)
            if slot_count == 0:
                return None
            skin_name = "default"
            skin_bones: list = []
            skin_ik: list = []
            skin_transform: list = []
            skin_path: list = []
        else:
            skin_name = self.read_string_ref() or "skin"
            nn = self.read_int_var(True)
            skin_bones = [bones[self.read_int_var(True)]["name"] for _ in range(nn)]
            nn = self.read_int_var(True)
            skin_ik = [self.read_int_var(True) for _ in range(nn)]  # indices; resolve later if needed
            nn = self.read_int_var(True)
            skin_transform = [self.read_int_var(True) for _ in range(nn)]
            nn = self.read_int_var(True)
            skin_path = [self.read_int_var(True) for _ in range(nn)]
            slot_count = self.read_int_var(True)

        attachments: dict[str, dict] = {}
        for _ in range(slot_count):
            slot_index = self.read_int_var(True)
            slot_name = slots[slot_index]["name"]
            slot_atts: dict = {}
            nn = self.read_int_var(True)
            for _ in range(nn):
                att_name = self.read_string_ref() or ""
                att = self._read_attachment(att_name, nonessential)
                if att is not None:
                    slot_atts[att_name] = att
            if slot_atts:
                attachments[slot_name] = slot_atts

        skin: dict[str, Any] = {"name": skin_name, "attachments": attachments}
        return skin

    def _read_attachment(self, attachment_name: str, nonessential: bool) -> Optional[dict]:
        name = self.read_string_ref()
        if name is None:
            name = attachment_name

        type_id = self.read_byte()
        scale = self.scale

        if type_id == self.ATTACHMENT_REGION:
            path = self.read_string_ref()
            rotation = self.read_float()
            x = self.read_float() * scale
            y = self.read_float() * scale
            scale_x = self.read_float()
            scale_y = self.read_float()
            width = self.read_float() * scale
            height = self.read_float() * scale
            color = self.read_color_rgba()
            if path is None:
                path = name
            att: dict[str, Any] = {"type": "region", "path": path, "width": width, "height": height}
            if name != attachment_name:
                att["name"] = name
            if rotation:
                att["rotation"] = rotation
            if x:
                att["x"] = x
            if y:
                att["y"] = y
            if scale_x != 1:
                att["scaleX"] = scale_x
            if scale_y != 1:
                att["scaleY"] = scale_y
            if color != "ffffffff":
                att["color"] = color
            return att

        if type_id == self.ATTACHMENT_BOUNDINGBOX:
            vertex_count = self.read_int_var(True)
            vertices = self._read_vertices(vertex_count)
            if nonessential:
                self.read_int()  # color
            att = {"type": "boundingbox", "vertexCount": vertex_count, "vertices": vertices}
            return att

        if type_id == self.ATTACHMENT_MESH:
            path = self.read_string_ref()
            color = self.read_color_rgba()
            vertex_count = self.read_int_var(True)
            uvs = [self.read_float() for _ in range(vertex_count * 2)]
            triangles = self._read_short_array()
            vertices = self._read_vertices(vertex_count)
            hull = self.read_int_var(True)
            edges = None
            width = height = 0.0
            if nonessential:
                edges = self._read_short_array()
                width = self.read_float() * scale
                height = self.read_float() * scale
            if path is None:
                path = name
            att = {
                "type": "mesh",
                "path": path,
                "uvs": uvs,
                "triangles": triangles,
                "vertices": vertices,
                "hull": hull,
            }
            if color != "ffffffff":
                att["color"] = color
            if edges is not None:
                att["edges"] = edges
            if width:
                att["width"] = width
            if height:
                att["height"] = height
            return att

        if type_id == self.ATTACHMENT_LINKEDMESH:
            path = self.read_string_ref()
            color = self.read_color_rgba()
            skin_name = self.read_string_ref()
            parent = self.read_string_ref()
            inherit_deform = self.read_boolean()
            width = height = 0.0
            if nonessential:
                width = self.read_float() * scale
                height = self.read_float() * scale
            if path is None:
                path = name
            att = {"type": "linkedmesh", "path": path, "parent": parent}
            if skin_name:
                att["skin"] = skin_name
            if not inherit_deform:
                att["deform"] = False
            if color != "ffffffff":
                att["color"] = color
            if width:
                att["width"] = width
            if height:
                att["height"] = height
            return att

        if type_id == self.ATTACHMENT_PATH:
            closed = self.read_boolean()
            constant_speed = self.read_boolean()
            vertex_count = self.read_int_var(True)
            vertices = self._read_vertices(vertex_count)
            lengths = [self.read_float() * scale for _ in range(vertex_count // 3)]
            if nonessential:
                self.read_int()  # color
            att = {
                "type": "path",
                "closed": closed,
                "constantSpeed": constant_speed,
                "vertexCount": vertex_count,
                "vertices": vertices,
                "lengths": lengths,
            }
            return att

        if type_id == self.ATTACHMENT_POINT:
            rotation = self.read_float()
            x = self.read_float() * scale
            y = self.read_float() * scale
            if nonessential:
                self.read_int()
            att = {"type": "point", "x": x, "y": y}
            if rotation:
                att["rotation"] = rotation
            return att

        if type_id == self.ATTACHMENT_CLIPPING:
            end_slot = self.read_int_var(True)
            vertex_count = self.read_int_var(True)
            vertices = self._read_vertices(vertex_count)
            if nonessential:
                self.read_int()
            att = {
                "type": "clipping",
                "end": end_slot,  # index; ideally resolve to name
                "vertexCount": vertex_count,
                "vertices": vertices,
            }
            return att

        raise ValueError(f"Unknown attachment type {type_id} at pos {self.pos}")

    def _read_vertices(self, vertex_count: int) -> list:
        if not self.read_boolean():
            # non-weighted
            return [self.read_float() * self.scale for _ in range(vertex_count * 2)]
        # weighted
        vertices: list = []
        for _ in range(vertex_count):
            bone_count = self.read_int_var(True)
            vertices.append(bone_count)
            for _ in range(bone_count):
                vertices.append(self.read_int_var(True))
                vertices.append(self.read_float() * self.scale)
                vertices.append(self.read_float() * self.scale)
                vertices.append(self.read_float())
        return vertices

    def _read_short_array(self) -> list[int]:
        n = self.read_int_var(True)
        arr = []
        for _ in range(n):
            arr.append((self.read_byte() << 8) | self.read_byte())
        return arr

    def _read_animation(
        self, bones, slots, iks, transforms, paths, events
    ) -> dict:
        """Simplified animation reader — keeps structure, may skip some curve detail."""
        anim: dict[str, Any] = {}

        # Slot timelines
        n = self.read_int_var(True)
        slots_tl: dict = {}
        for _ in range(n):
            slot_idx = self.read_int_var(True)
            slot_name = slots[slot_idx]["name"]
            nn = self.read_int_var(True)
            slot_timelines: dict = {}
            for _ in range(nn):
                timeline_type = self.read_byte()
                frame_count = self.read_int_var(True)
                if timeline_type == 0:  # attachment
                    frames = []
                    for fi in range(frame_count):
                        time = self.read_float()
                        name = self.read_string_ref()
                        frames.append({"time": time, "name": name})
                    slot_timelines["attachment"] = frames
                elif timeline_type == 1:  # color RGBA
                    frames = []
                    for fi in range(frame_count):
                        time = self.read_float()
                        color = self.read_color_rgba()
                        frame = {"time": time, "color": color}
                        if fi < frame_count - 1:
                            self._read_curve(frame)
                        frames.append(frame)
                    slot_timelines["color"] = frames
                elif timeline_type == 2:  # two-color
                    frames = []
                    for fi in range(frame_count):
                        time = self.read_float()
                        light = self.read_color_rgba()
                        dark = self.read_color_rgba()
                        frame = {"time": time, "light": light, "dark": dark}
                        if fi < frame_count - 1:
                            self._read_curve(frame)
                        frames.append(frame)
                    slot_timelines["twoColor"] = frames
                else:
                    raise ValueError(f"Unknown slot timeline type {timeline_type}")
            if slot_timelines:
                slots_tl[slot_name] = slot_timelines
        if slots_tl:
            anim["slots"] = slots_tl

        # Bone timelines
        n = self.read_int_var(True)
        bones_tl: dict = {}
        for _ in range(n):
            bone_idx = self.read_int_var(True)
            bone_name = bones[bone_idx]["name"]
            nn = self.read_int_var(True)
            bone_timelines: dict = {}
            for _ in range(nn):
                timeline_type = self.read_byte()
                frame_count = self.read_int_var(True)
                # 0 rotate, 1 translate, 2 scale, 3 shear
                type_names = {0: "rotate", 1: "translate", 2: "scale", 3: "shear"}
                tname = type_names.get(timeline_type, f"type{timeline_type}")
                frames = []
                for fi in range(frame_count):
                    time = self.read_float()
                    if timeline_type == 0:
                        frame = {"time": time, "angle": self.read_float()}
                    else:
                        x = self.read_float()
                        y = self.read_float()
                        if timeline_type == 1:  # translate
                            x *= self.scale
                            y *= self.scale
                        frame = {"time": time, "x": x, "y": y}
                    if fi < frame_count - 1:
                        self._read_curve(frame)
                    frames.append(frame)
                bone_timelines[tname] = frames
            if bone_timelines:
                bones_tl[bone_name] = bone_timelines
        if bones_tl:
            anim["bones"] = bones_tl

        # IK timelines
        n = self.read_int_var(True)
        if n:
            ik_tl: dict = {}
            for _ in range(n):
                ik_idx = self.read_int_var(True)
                ik_name = iks[ik_idx]["name"] if ik_idx < len(iks) else str(ik_idx)
                frame_count = self.read_int_var(True)
                frames = []
                for fi in range(frame_count):
                    time = self.read_float()
                    mix = self.read_float()
                    softness = self.read_float() * self.scale
                    bend = self.read_sbyte()
                    compress = self.read_boolean()
                    stretch = self.read_boolean()
                    frame = {"time": time, "mix": mix, "softness": softness}
                    if bend != 1:
                        frame["bendPositive"] = bend > 0
                    if compress:
                        frame["compress"] = True
                    if stretch:
                        frame["stretch"] = True
                    if fi < frame_count - 1:
                        self._read_curve(frame)
                    frames.append(frame)
                ik_tl[ik_name] = frames
            anim["ik"] = ik_tl

        # Transform timelines
        n = self.read_int_var(True)
        if n:
            tr_tl: dict = {}
            for _ in range(n):
                idx = self.read_int_var(True)
                name = transforms[idx]["name"] if idx < len(transforms) else str(idx)
                frame_count = self.read_int_var(True)
                frames = []
                for fi in range(frame_count):
                    time = self.read_float()
                    frame = {
                        "time": time,
                        "rotateMix": self.read_float(),
                        "translateMix": self.read_float(),
                        "scaleMix": self.read_float(),
                        "shearMix": self.read_float(),
                    }
                    if fi < frame_count - 1:
                        self._read_curve(frame)
                    frames.append(frame)
                tr_tl[name] = frames
            anim["transform"] = tr_tl

        # Path timelines
        n = self.read_int_var(True)
        if n:
            path_tl: dict = {}
            for _ in range(n):
                idx = self.read_int_var(True)
                name = paths[idx]["name"] if idx < len(paths) else str(idx)
                nn = self.read_int_var(True)
                timelines: dict = {}
                for _ in range(nn):
                    timeline_type = self.read_byte()  # 0 position, 1 spacing, 2 mix
                    frame_count = self.read_int_var(True)
                    tnames = {0: "position", 1: "spacing", 2: "mix"}
                    tname = tnames.get(timeline_type, f"t{timeline_type}")
                    frames = []
                    for fi in range(frame_count):
                        time = self.read_float()
                        if timeline_type == 2:
                            frame = {
                                "time": time,
                                "rotateMix": self.read_float(),
                                "translateMix": self.read_float(),
                            }
                        else:
                            frame = {"time": time, tname: self.read_float()}
                        if fi < frame_count - 1:
                            self._read_curve(frame)
                        frames.append(frame)
                    timelines[tname] = frames
                path_tl[name] = timelines
            anim["path"] = path_tl

        # Deform
        n = self.read_int_var(True)
        if n:
            deform: dict = {}
            for _ in range(n):
                skin_idx = self.read_int_var(True)
                # We don't resolve skin names easily here — store index
                skin_key = f"skin{skin_idx}"
                skin_deform: dict = {}
                nn = self.read_int_var(True)
                for _ in range(nn):
                    slot_idx = self.read_int_var(True)
                    slot_name = slots[slot_idx]["name"]
                    slot_deform: dict = {}
                    nnn = self.read_int_var(True)
                    for _ in range(nnn):
                        att_name = self.read_string_ref() or ""
                        frame_count = self.read_int_var(True)
                        frames = []
                        for fi in range(frame_count):
                            time = self.read_float()
                            end = self.read_int_var(True)
                            if end != 0:
                                start = self.read_int_var(True)
                                verts = [self.read_float() * self.scale for _ in range(end)]
                                frame = {"time": time, "offset": start, "vertices": verts}
                            else:
                                frame = {"time": time}
                            if fi < frame_count - 1:
                                self._read_curve(frame)
                            frames.append(frame)
                        slot_deform[att_name] = frames
                    skin_deform[slot_name] = slot_deform
                deform[skin_key] = skin_deform
            anim["deform"] = deform

        # Draw order
        n = self.read_int_var(True)
        if n:
            draw_order = []
            for _ in range(n):
                time = self.read_float()
                offset_count = self.read_int_var(True)
                offsets = []
                for _ in range(offset_count):
                    slot_idx = self.read_int_var(True)
                    offset = self.read_int_var(False)
                    offsets.append({"slot": slots[slot_idx]["name"], "offset": offset})
                draw_order.append({"time": time, "offsets": offsets})
            anim["drawOrder"] = draw_order

        # Event timelines
        n = self.read_int_var(True)
        if n:
            ev_frames = []
            for _ in range(n):
                time = self.read_float()
                event_idx = self.read_int_var(True)
                ev_name = events[event_idx]["name"] if event_idx < len(events) else str(event_idx)
                frame: dict[str, Any] = {"time": time, "name": ev_name}
                frame["int"] = self.read_int_var(False)
                frame["float"] = self.read_float()
                if self.read_boolean():
                    frame["string"] = self.read_string()
                # audio volume/balance if event has audio — simplified skip check
                ev_frames.append(frame)
            anim["events"] = ev_frames

        return anim

    def _read_curve(self, frame: dict) -> None:
        """Spine 3.8 JSON curve format (NOT 4.x array form).
        linear  → omit
        stepped → "curve": "stepped"
        bezier  → "curve": cx1, "c2": cy1, "c3": cx2, "c4": cy2
        """
        curve_type = self.read_byte()
        if curve_type == 0:  # linear
            return
        if curve_type == 1:  # stepped
            frame["curve"] = "stepped"
            return
        if curve_type == 2:  # bezier — 3.8 uses separate fields, not an array
            frame["curve"] = self.read_float()
            frame["c2"] = self.read_float()
            frame["c3"] = self.read_float()
            frame["c4"] = self.read_float()
            return
        # unknown — leave as linear


def convert_file(src: Path, dst: Path) -> None:
    data = src.read_bytes()
    reader = Spine38BinaryReader(data)
    doc = reader.read_skeleton_data()
    # leftover bytes warning
    leftover = len(data) - reader.pos
    if leftover > 0:
        doc.setdefault("_meta", {})["leftover_bytes"] = leftover
        doc["_meta"]["pos"] = reader.pos
    dst.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"OK  {src.name} → {dst.name}  (pos={reader.pos}/{len(data)}, leftover={leftover})")


def main():
    if len(sys.argv) < 2:
        print("Usage: spine38_binary_to_json.py <file.skel> [out.json]")
        print("   or: spine38_binary_to_json.py <dir>")
        sys.exit(1)
    src = Path(sys.argv[1])
    if src.is_dir():
        for p in sorted(src.glob("*.skel")) + sorted(src.glob("*.json")):
            # only binary
            head = p.read_bytes()[:4]
            if head.startswith(b"{") or head.startswith(b"["):
                continue
            out = p.with_name(p.stem + "_converted.json")
            try:
                convert_file(p, out)
            except Exception as e:
                print(f"FAIL {p.name}: {type(e).__name__}: {e}")
        return

    dst = Path(sys.argv[2]) if len(sys.argv) > 2 else src.with_name(src.stem + "_converted.json")
    convert_file(src, dst)


if __name__ == "__main__":
    main()
