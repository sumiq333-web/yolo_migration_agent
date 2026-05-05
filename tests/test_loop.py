"""Tests for YOLO scanning."""

from __future__ import annotations

import json

from tools.yolo_tools import sacn_yolo_project


def test_scan_yolo_separates_dirs_and_files(tmp_path):
    (tmp_path / "z_dir").mkdir()
    (tmp_path / "a_file.txt").write_text("a", encoding="utf-8")
    (tmp_path / "m_file.txt").write_text("m", encoding="utf-8")

    for index in range(40):
        (tmp_path / f"extra_{index:02d}.txt").write_text("x", encoding="utf-8")

    data = json.loads(sacn_yolo_project(tmp_path))

    assert "dirs" in data
    assert "files" in data
    assert "z_dir" in data["dirs"]
    assert "a_file.txt" in data["files"]
    assert len(data["dirs"]) + len(data["files"]) <= 30
