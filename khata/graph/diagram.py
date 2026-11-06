"""Print the workflow graph as Mermaid (for the docs): `make graph`."""

from __future__ import annotations

from unittest.mock import MagicMock

from khata.graph.workflow import Deps, build_graph


def mermaid() -> str:
    graph = build_graph(Deps(MagicMock(), MagicMock(), MagicMock())).compile()
    return graph.get_graph().draw_mermaid()


if __name__ == "__main__":
    print(mermaid())
