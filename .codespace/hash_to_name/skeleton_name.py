import json
import re
from collections import Counter

GENERIC_SKELETON_NAMES = {
    "root", "bone", "bone1", "bone2", "bone3", "bone4", "bone5", "bone6",
    "bone7", "bone8", "bone9", "default", "skeleton", "main", "body", "slot",
    "att", "attachment", "attachments", "image", "images", "temp", "tmp",
    "name", "hash", "true", "false", "start", "end", "time", "color", "curve",
    "stepped", "x", "y", "width", "height", "spine", "version", "animations",
    "bones", "slots", "skins", "events", "ik", "transform", "path"
}

_GARBAGE_UPPER_RE = re.compile(r'[A-Z]{3,}')
_SHORT_HASH_RE = re.compile(r'_([0-9a-fA-F]{8})$')


def _looks_like_garbage_token(tok: str) -> bool:
    if not tok:
        return True
    if '+' in tok or '/' in tok or '=' in tok:
        return True
    upper = sum(1 for c in tok if c.isupper())
    lower = sum(1 for c in tok if c.islower())
    if len(tok) >= 12 and upper >= 5 and lower >= 5 and '_' not in tok:
        return True
    if _GARBAGE_UPPER_RE.search(tok):
        return True
    return False


def is_hashed_name(name):
    base = re.split(r'\.', str(name))[-1] if '.' in str(name) else str(name)
    base = re.split(r'[/\\]', base)[-1]
    return len(base) >= 32 and all(c in "0123456789abcdefABCDEF" for c in base)


def strip_short_hash(stem: str) -> str:
    if not stem:
        return stem
    stem = re.sub(r'[\r\n\t\x00-\x1f]+', '_', stem)
    stem = re.sub(r'^(?:\\u[0-9a-fA-F]{4}|u[0-9a-fA-F]{4}|[^a-zA-Z0-9])+', '', stem)
    return _SHORT_HASH_RE.sub('', stem).strip('_')


def infer_name_from_binary(data: bytes) -> str:
    pattern = re.compile(rb'[a-zA-Z][a-zA-Z0-9_/]{3,50}')
    candidates = []
    for m in pattern.finditer(data):
        tok = m.group().decode('ascii', errors='ignore')
        for part in tok.split('/'):
            base = re.sub(r'[_\-]\d+$', '', part)
            base = re.sub(r'\d+$', '', base)
            base = base.strip('_')
            if not base or len(base) < 4:
                continue
            if is_hashed_name(base):
                continue
            if base.lower() in GENERIC_SKELETON_NAMES:
                continue
            if _looks_like_garbage_token(base):
                continue
            candidates.append(base)
    if not candidates:
        return ""
    counts = Counter(candidates)
    def _score(item):
        tok, cnt = item
        return cnt + (2 if '_' in tok else 0) + min(len(tok), 20) / 20.0
    best = max(counts.items(), key=_score)[0]
    result = strip_short_hash(best)
    if result and not is_hashed_name(result) and not _looks_like_garbage_token(result):
        return result
    return ""


def infer_name_from_dict(data, fallback_stem: str) -> str:
    if not is_hashed_name(fallback_stem):
        return strip_short_hash(fallback_stem)
    if not isinstance(data, dict):
        return strip_short_hash(fallback_stem)
    sk_name = data.get("skeleton", {}).get("name", "")
    if sk_name:
        sk_clean = strip_short_hash(str(sk_name))
        if sk_clean and not is_hashed_name(sk_clean) and sk_clean.lower() not in GENERIC_SKELETON_NAMES:
            return sk_clean
    candidates = []
    for b in data.get("bones", []):
        if not isinstance(b, dict):
            continue
        bname = b.get("name", "")
        if "/" in bname:
            bname = bname.split("/")[0]
        bname = strip_short_hash(bname)
        bname = re.sub(r"_\d+$", "", bname)
        bname = re.sub(r"\d+$", "", bname)
        if bname and len(bname) >= 3 and not is_hashed_name(bname) and bname.lower() not in GENERIC_SKELETON_NAMES:
            candidates.append(bname)
    for skin in data.get("skins", []):
        if not isinstance(skin, dict):
            continue
        atts_dict = skin.get("attachments", {})
        if not isinstance(atts_dict, dict):
            continue
        for slot_name, slot_atts in atts_dict.items():
            s_base = slot_name.split("/")[0] if "/" in slot_name else slot_name
            s_base = strip_short_hash(s_base)
            s_base = re.sub(r"_\d+$", "", s_base)
            s_base = re.sub(r"\d+$", "", s_base)
            if s_base and len(s_base) >= 3 and not is_hashed_name(s_base) and s_base.lower() not in GENERIC_SKELETON_NAMES:
                candidates.append(s_base)
            if isinstance(slot_atts, dict):
                for att_key in slot_atts.keys():
                    if att_key == "att":
                        continue
                    a_base = att_key.split("/")[0] if "/" in att_key else att_key
                    a_base = strip_short_hash(a_base)
                    a_base = re.sub(r"_\d+$", "", a_base)
                    a_base = re.sub(r"\d+$", "", a_base)
                    if a_base and len(a_base) >= 3 and not is_hashed_name(a_base) and a_base.lower() not in GENERIC_SKELETON_NAMES:
                        candidates.append(a_base)
    if candidates:
        most_common = Counter(candidates).most_common(1)[0][0]
        res = strip_short_hash(most_common)
        if res.startswith("ckground"):
            res = "ba" + res
        return res
    try:
        raw_text = json.dumps(data)
        tokens = re.findall(r"[a-zA-Z][a-zA-Z0-9_]{2,}", raw_text)
        for tok in tokens:
            if is_hashed_name(tok):
                continue
            for part in tok.split("/"):
                p_clean = strip_short_hash(part)
                p_clean = re.sub(r"_\d+$", "", p_clean)
                p_clean = re.sub(r"\d+$", "", p_clean)
                if p_clean and len(p_clean) >= 3 and p_clean.lower() not in GENERIC_SKELETON_NAMES and not is_hashed_name(p_clean):
                    candidates.append(p_clean)
    except Exception:
        pass
    if candidates:
        most_common = Counter(candidates).most_common(1)[0][0]
        res = strip_short_hash(most_common)
        if res.startswith("ckground"):
            res = "ba" + res
        return res
    return strip_short_hash(fallback_stem)