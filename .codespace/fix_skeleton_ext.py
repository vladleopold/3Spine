#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Некоторые игры отдают бинарный Spine-скелет с расширением .json
# (например pot.json — внутри сигнатура \x1c "ya0" и версия 3.8.99).
# Из-за этого шаги Spine их не видят. Возвращаем расширение .skel.
#
# Использование: python3 .codespace/fix_skeleton_ext.py <каталог>
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from prepare_input import fix_misnamed_json  # noqa: E402


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("нужен каталог: python3 .codespace/fix_skeleton_ext.py <каталог>")
    root = os.path.abspath(sys.argv[1])
    loglines = []
    fixed = fix_misnamed_json(root, loglines)
    print("fix-skeleton-ext: бинарных skel под именем .json исправлено: %d" % fixed)
    for line in loglines[:30]:
        print("  " + line)


if __name__ == "__main__":
    main()
