"""Render a spending chart as a PNG (matplotlib, headless)."""

from __future__ import annotations

import io


def render_category_chart(by_category: dict[str, float], title: str) -> bytes | None:
    """Horizontal bar chart of spending by category. None if there's nothing."""
    if not by_category:
        return None

    # Import lazily and force the non-GUI backend (server has no display).
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    items = sorted(by_category.items(), key=lambda kv: kv[1])
    labels = [k for k, _ in items]
    values = [v for _, v in items]

    fig, ax = plt.subplots(figsize=(7, max(2.5, 0.5 * len(labels) + 1)))
    ax.barh(labels, values, color="#4C78A8")
    ax.set_title(title)
    ax.set_xlabel("Spent")
    for i, v in enumerate(values):
        ax.text(v, i, f" {v:,.0f}", va="center")
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120)
    plt.close(fig)
    return buf.getvalue()
