"""Conservative cross-page table associations; original rows/locators never change."""


def link_pdf_tables(content):
    pages = {}
    for block in content.blocks:
        if (
            block.type == "table"
            and block.table_rows
            and block.locators[0].page
            and block.locators[0].bbox
        ):
            pages.setdefault(block.locators[0].page, []).append(block)
    links = []
    for page, values in sorted(pages.items()):
        if page + 1 not in pages:
            continue
        previous = max(values, key=lambda b: b.locators[0].bbox[3])
        following = min(pages[page + 1], key=lambda b: b.locators[0].bbox[1])
        a, b = previous.locators[0].bbox, following.locators[0].bbox
        header_a = [" ".join(c.split()) for c in previous.table_rows[0]]
        header_b = [" ".join(c.split()) for c in following.table_rows[0]]
        if a[3] < 0.72 or b[1] > 0.28 or abs(a[0] - b[0]) > 0.03 or abs(a[2] - b[2]) > 0.03:
            continue
        if (
            header_a != header_b
            or len(header_a) < 2
            or not all(header_a)
            or len(previous.table_rows) < 2
            or len(following.table_rows) < 2
        ):
            continue
        series = previous.metadata.get("table_series", previous.block_id)
        previous.metadata.update(
            table_series=series,
            continuation_next=following.block_id,
            continuation_method="matching_header_and_page_geometry",
        )
        following.metadata.update(
            table_series=series,
            continuation_previous=previous.block_id,
            continuation_method="matching_header_and_page_geometry",
        )
        links.append(
            {
                "previous": previous.block_id,
                "next": following.block_id,
                "pages": [page, page + 1],
                "verification": "heuristic_only",
            }
        )
    if links:
        content.metadata["table_continuations"] = links
    return content
