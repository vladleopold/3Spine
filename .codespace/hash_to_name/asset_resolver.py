"""Asset name resolution for hashed/obfuscated filenames in Spine projects.

Detects hashed filenames (32+ hex chars) and resolves them to real names
using the first line of atlas files.
"""
import os
import re

IMAGE_EXTS = ('.png', '.webp', '.jpg', '.jpeg', '.bmp', '.avif', '.tiff', '.tif', '.gif')
ATLAS_EXTS = ('.atlas', '.atlas.txt', '.atlas.json')


def is_hashed(text):
    """Check if a string looks like a hex hash (32+ hex chars).

    The extension (if any) is stripped before checking, so both ``abc123...``
    and ``abc123....png`` are recognized as hashed.
    """
    if not text:
        return False
    base = os.path.splitext(text)[0] if '.' in text else text
    if len(base) >= 32 and re.fullmatch(r'[0-9a-fA-F]+', base):
        return True
    return False


def is_hashed_file(filename):
    """Check if a filename (basename, with or without extension) is hashed.

    Strips the extension first, then delegates to :func:`is_hashed`, so a
    hashed base name with any image/atlas extension is detected.
    """
    base = os.path.splitext(filename)[0] if '.' in filename else filename
    return is_hashed(base)


def read_atlas_first_line(atlas_path):
    """Read the first non-empty line from an atlas file."""
    try:
        with open(atlas_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line:
                    return line
    except Exception:
        pass
    return None


def resolve_texture_name(tex_name, atlas_path=None, atlas_dir=None):
    """Resolve a potentially hashed texture name to its real name.

    If the atlas file has a hashed name, reads its first line for the real asset name.
    Falls through from ``atlas_path`` to ``atlas_dir`` when the explicit atlas
    yields no result (missing file, empty first line, etc.).
    """
    if not is_hashed(tex_name):
        return tex_name

    # Collect candidate atlas files: the explicit one first, then any hashed
    # atlas in atlas_dir.  A non-result from atlas_path never blocks atlas_dir.
    candidates = []
    if atlas_path and is_hashed_file(os.path.basename(atlas_path)):
        candidates.append(atlas_path)
    if atlas_dir:
        try:
            entries = sorted(os.listdir(atlas_dir))
        except OSError:
            entries = []
        for f in entries:
            if f.endswith(ATLAS_EXTS) and is_hashed_file(f):
                candidates.append(os.path.join(atlas_dir, f))

    for a_path in candidates:
        real = read_atlas_first_line(a_path)
        if real:
            return real

    return tex_name


def find_image_file(directory, texture_name, atlas_path=None):
    """Find an image file by texture name, handling hashed names.

    Returns (full_path, actual_filename) or (None, None).
    """
    # Try texture name as-is with extensions
    for ext in IMAGE_EXTS:
        path = os.path.join(directory, texture_name + ext)
        if os.path.exists(path):
            return path, texture_name + ext

    # Try texture name exactly (if it already has an image extension)
    if os.path.splitext(texture_name)[1].lower() in IMAGE_EXTS:
        path = os.path.join(directory, texture_name)
        if os.path.exists(path):
            return path, texture_name

    # If texture name was resolved from a hashed atlas, try hashed version on disk
    if atlas_path and is_hashed_file(os.path.basename(atlas_path)):
        hashed_base = os.path.splitext(os.path.basename(atlas_path))[0]
        for ext in IMAGE_EXTS:
            path = os.path.join(directory, hashed_base + ext)
            if os.path.exists(path):
                return path, hashed_base + ext

        # Also try atlas basename as image name
        atlas_base = os.path.splitext(os.path.basename(atlas_path))[0]
        for ext in IMAGE_EXTS:
            path = os.path.join(directory, atlas_base + ext)
            if os.path.exists(path):
                return path, atlas_base + ext

    # Fallback: search the directory for any hashed image file.  This runs
    # even when ``texture_name`` itself is not hashed: if the exact-name
    # lookup above failed, the directory is assumed to hold hashed assets and
    # the first hashed image is returned as the best available match.
    try:
        entries = os.listdir(directory)
    except OSError:
        return None, None
    for f in entries:
        if f.lower().endswith(IMAGE_EXTS) and is_hashed_file(f):
            return os.path.join(directory, f), f

    return None, None


def resolve_project_name(json_path):
    """Get the project/skeleton name from a JSON file path or content."""
    import json as json_mod
    try:
        with open(json_path, 'r', encoding='utf-8') as f:
            data = json_mod.load(f)
        if isinstance(data, dict):
            skel = data.get('skeleton', {})
            if isinstance(skel, dict) and skel.get('name'):
                return skel['name']
            if data.get('name'):
                return data['name']
    except Exception:
        pass
    from pathlib import Path
    return Path(json_path).stem


def find_spine_files(root_dir):
    """Recursively find all Spine-related files in a directory tree.

    Returns dict: {'json': [...], 'atlas': [...], 'images': [...]}
    """
    result = {'json': [], 'atlas': [], 'images': []}
    for dirpath, _, filenames in os.walk(root_dir):
        for fn in filenames:
            lower = fn.lower()
            full = os.path.join(dirpath, fn)
            if lower.endswith('.json'):
                result['json'].append(full)
            elif lower.endswith(ATLAS_EXTS):
                result['atlas'].append(full)
            elif lower.endswith(IMAGE_EXTS):
                result['images'].append(full)
    return result


def _dir_depth(path, root):
    """Number of directory levels under root."""
    try:
        rel = os.path.relpath(path, root)
        return len(rel.split(os.sep))
    except Exception:
        return 999


def _dir_distance(d1, d2):
    """Directory distance: how many levels apart.

    Uses ``os.path.realpath`` comparison instead of ``os.path.samefile`` so
    broken symlinks (which make ``samefile`` raise ``OSError``) do not break
    distance computation.
    """
    try:
        if os.path.realpath(d1) == os.path.realpath(d2):
            return 0
    except OSError:
        pass
    try:
        common = os.path.commonpath([d1, d2])
        d1_rel = os.path.relpath(d1, common).split(os.sep)
        d2_rel = os.path.relpath(d2, common).split(os.sep)
        # Drop the '.' entry that commonpath emits for the directory itself
        # (e.g. relpath(d, common) == '.'), otherwise a parent/child pair is
        # reported as distance 2 instead of 1.
        return len([p for p in d1_rel if p not in ('.', '..')]) + len(
            [p for p in d2_rel if p not in ('.', '..')]
        )
    except Exception:
        return 999


def group_spine_projects(root_dir):
    """Group found files into Spine projects by directory proximity.

    Each Spine JSON defines a project. Associated atlas + image files
    are found recursively and assigned to the closest JSON by directory distance.

    Returns list of dicts: [{'name': str, 'json': path, 'atlases': [...}, 'images': [...]}, ...]
    """
    files = find_spine_files(root_dir)
    json_list = files['json']

    # Validate JSONs
    valid_jsons = []
    for jp in json_list:
        try:
            import json as json_mod
            with open(jp, 'r', encoding='utf-8') as f:
                data = json_mod.load(f)
            if 'skeleton' in data:
                valid_jsons.append(jp)
        except Exception:
            continue

    # Build pool of unassigned files
    unassigned_atlases = list(files['atlas'])
    unassigned_images = list(files['images'])
    assigned = set()

    projects = []
    for json_path in valid_jsons:
        project_dir = os.path.dirname(json_path)
        skeleton_name = resolve_project_name(json_path)

        # Find closest atlas for each unassigned atlas
        atlases = []
        remaining_atlases = []
        for a in unassigned_atlases:
            a_dir = os.path.dirname(a)
            if os.path.samefile(a_dir, project_dir) or os.path.commonpath([a_dir, project_dir]) == project_dir:
                atlases.append(a)
                assigned.add(a)
            else:
                remaining_atlases.append(a)

        # Also pick atlases from subdirectories of project_dir
        for a in remaining_atlases[:]:
            a_dir = os.path.dirname(a)
            try:
                common = os.path.commonpath([a_dir, project_dir])
                if os.path.samepath(common, project_dir):
                    atlases.append(a)
                    assigned.add(a)
                    remaining_atlases.remove(a)
            except Exception:
                pass

        # Find closest images
        images = []
        remaining_images = []
        for img in unassigned_images:
            img_dir = os.path.dirname(img)
            if os.path.samefile(img_dir, project_dir) or os.path.commonpath([img_dir, project_dir]) == project_dir:
                images.append(img)
                assigned.add(img)
            else:
                remaining_images.append(img)

        for img in remaining_images[:]:
            img_dir = os.path.dirname(img)
            try:
                common = os.path.commonpath([img_dir, project_dir])
                if os.path.samepath(common, project_dir):
                    images.append(img)
                    assigned.add(img)
                    remaining_images.remove(img)
            except Exception:
                pass

        # Fallback: assign by minimum directory distance
        for a in remaining_atlases[:]:
            if a in assigned:
                continue
            dist = _dir_distance(os.path.dirname(a), project_dir)
            min_dist = dist
            best = True
            for other in valid_jsons:
                if other == json_path:
                    continue
                other_dist = _dir_distance(os.path.dirname(a), os.path.dirname(other))
                if other_dist < min_dist:
                    best = False
                    break
            if best and dist <= 4:
                atlases.append(a)
                assigned.add(a)

        for img in remaining_images[:]:
            if img in assigned:
                continue
            dist = _dir_distance(os.path.dirname(img), project_dir)
            best = True
            for other in valid_jsons:
                if other == json_path:
                    continue
                other_dist = _dir_distance(os.path.dirname(img), os.path.dirname(other))
                if other_dist < dist:
                    best = False
                    break
            if best and dist <= 4:
                images.append(img)
                assigned.add(img)

        projects.append({
            'name': skeleton_name,
            'json': json_path,
            'atlases': atlases,
            'images': images,
            'dir': project_dir,
        })

        unassigned_atlases = [a for a in unassigned_atlases if a not in assigned]
        unassigned_images = [i for i in unassigned_images if i not in assigned]

    return projects