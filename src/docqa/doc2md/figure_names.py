"""Readable per-occurrence image aliases; content-addressed assets remain canonical."""

import shutil
from pathlib import Path

from docqa.doc2md.common import safe_slug


def name_figures(content, context):
    assets = {asset.asset_id: asset for asset in content.assets}
    # Enrichment populates context assets before the final IR combines them.
    assets.update(context.image_assets)
    counters = {}
    for figure in content.figures:
        page = figure.locator.page or figure.locator.slide
        key = str(page) if page else "NA"
        counters[key] = counters.get(key, 0) + 1
        title = figure.caption or (figure.vision.image_type if figure.vision else "image")
        label = f"{safe_slug(Path(context.source_name).stem)[:45]}_Page{key}图{counters[key]:03d}_Figure{safe_slug(title)[:45]}"
        relative = f"images/figures/{label}__{figure.figure_id[:12]}.png"
        source = context.work_dir / assets[figure.asset_id].relative_path
        target = context.work_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        figure.metadata.update(trace_name=label, trace_image_path=relative)
