from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .config import active_bib_files, resolve_path


def render_bibliography_pdf(config: dict, output_dir: str | None = None) -> Path:
    build_dir = resolve_path(config, output_dir or "build/bibliography-check")
    build_dir.mkdir(parents=True, exist_ok=True)

    copied_bibs: dict[str, dict] = {}
    for category, bib_config in active_bib_files(config).items():
        source_path = resolve_path(config, bib_config["path"])
        target_name = f"{category}.bib"
        target_path = build_dir / target_name
        source_text = source_path.read_text(encoding="utf-8")
        target_path.write_text(_sanitize_bib_for_compile(source_text), encoding="utf-8")
        copied_bibs[category] = {
            "bib_name": target_name,
            "label": bib_config.get("label", category.replace("_", " ").title()),
        }

    tex_path = build_dir / "bibliography_check.tex"
    tex_path.write_text(_render_tex(copied_bibs), encoding="utf-8")

    _run(["pdflatex", "-interaction=nonstopmode", tex_path.name], build_dir)
    for aux_path in sorted(build_dir.glob("bu*.aux")):
        _run(["bibtex", aux_path.stem], build_dir)
    _run(["pdflatex", "-interaction=nonstopmode", tex_path.name], build_dir)
    _run(["pdflatex", "-interaction=nonstopmode", tex_path.name], build_dir)

    pdf_path = build_dir / "bibliography_check.pdf"
    if not pdf_path.exists():
        raise RuntimeError(f"Expected PDF was not created: {pdf_path}")
    return pdf_path


def _run(command: list[str], cwd: Path) -> None:
    completed = subprocess.run(
        command,
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"Command failed: {' '.join(command)}\n"
            f"stdout:\n{completed.stdout}\n\nstderr:\n{completed.stderr}"
        )


def _sanitize_bib_for_compile(source_text: str) -> str:
    sanitized_lines = []
    for line in source_text.splitlines():
        if line.lstrip().startswith("%"):
            continue
        line = re.sub(r",\s+and\b", " and", line)
        sanitized_lines.append(line)
    return "\n".join(sanitized_lines) + "\n"


def _render_tex(copied_bibs: dict[str, dict]) -> str:
    sections = []
    for category, bib_info in copied_bibs.items():
        sections.append(
            "\n".join(
                [
                    f"\\section*{{{bib_info['label']}}}",
                    "\\begin{bibunit}[unsrt]",
                    "\\nocite{*}",
                    f"\\putbib[{Path(bib_info['bib_name']).stem}]",
                    "\\end{bibunit}",
                ]
            )
        )

    return "\n".join(
        [
            r"\documentclass[11pt]{article}",
            r"\usepackage[T1]{fontenc}",
            r"\usepackage[utf8]{inputenc}",
            r"\usepackage[margin=1in]{geometry}",
            r"\usepackage{bibunits}",
            r"\usepackage{hyperref}",
            r"\setlength{\parindent}{0pt}",
            r"\setlength{\parskip}{0.6em}",
            r"\begin{document}",
            r"\begin{center}",
            r"{\LARGE Bibliography Compile Check}\\",
            r"\end{center}",
            r"\tableofcontents",
            *sections,
            r"\end{document}",
            "",
        ]
    )
