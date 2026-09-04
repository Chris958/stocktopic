from __future__ import annotations

from . import theme_graph

# Generic taxonomies are useful as context/aliases but are too broad to become
# investable theme nodes by themselves. Concrete children such as 猪肉/白羽鸡 remain valid.
EXTRA_BROAD_PARENT_TAGS = {
    "大农业",
    "养殖",
    "畜牧",
    "畜牧养殖",
    "农林牧渔",
    "种植业",
}

_installed = False


def install_theme_taxonomy() -> None:
    global _installed
    if _installed:
        return
    _installed = True
    theme_graph.BROAD_PARENT_TAGS.update(EXTRA_BROAD_PARENT_TAGS)
