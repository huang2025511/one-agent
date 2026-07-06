"""Chart Generation — generate charts and diagrams from data.

Provides:
  - Line charts, bar charts, pie charts, scatter plots
  - Mermaid diagrams (flowchart, sequence, class, state, ER)
  - Data-to-chart: describe data in natural language, get chart
  - Output as ASCII, SVG, or Mermaid markdown
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class ChartGenerator:
    """Generate charts and diagrams from data descriptions.

    Supports:
      - ASCII charts (no dependency, always works)
      - Mermaid diagrams (rendered by most markdown renderers)
      - SVG charts (via matplotlib, optional)
    """

    name = "chart"
    description = "Generate charts and diagrams from data"

    def __init__(self):
        self._style = "mermaid"  # ascii, mermaid, or svg

    # --------------------------------------------------- Mermaid diagrams

    def generate_mermaid(
        self, chart_type: str, data: Dict[str, Any],
    ) -> str:
        """Generate a Mermaid diagram from data.

        Args:
            chart_type: flowchart, sequence, class, state, er, pie, gantt, timeline
            data: chart-specific data
        """
        if chart_type == "flowchart":
            return self._mermaid_flowchart(data)
        elif chart_type == "sequence":
            return self._mermaid_sequence(data)
        elif chart_type == "class":
            return self._mermaid_class(data)
        elif chart_type == "pie":
            return self._mermaid_pie(data)
        elif chart_type == "gantt":
            return self._mermaid_gantt(data)
        elif chart_type == "timeline":
            return self._mermaid_timeline(data)
        elif chart_type == "mindmap":
            return self._mermaid_mindmap(data)
        return "```mermaid\ngraph TD\n  A[Unsupported chart type]\n```"

    def _mermaid_flowchart(self, data: Dict[str, Any]) -> str:
        """Generate a flowchart."""
        direction = data.get("direction", "TD")  # TD, LR, BT, RL
        nodes = data.get("nodes", [])
        edges = data.get("edges", [])
        title = data.get("title", "")

        lines = ["```mermaid", f"graph {direction}"]
        if title:
            lines.append(f"  %% {title}")

        node_ids = set()
        for node in nodes:
            nid = node.get("id", "").replace(" ", "_")
            label = node.get("label", nid)
            shape = node.get("shape", "rect")  # rect, round, diamond, circle

            shape_map = {"rect": f"{nid}[{label}]", "round": f"{nid}({label})",
                        "diamond": f"{nid}{{{label}}}", "circle": f"{nid}(({label}))"}
            lines.append(f"  {shape_map.get(shape, shape_map['rect'])}")
            node_ids.add(nid)

        for edge in edges:
            src = edge.get("from", "")
            dst = edge.get("to", "")
            label = edge.get("label", "")
            if label:
                lines.append(f"  {src} -->|{label}| {dst}")
            else:
                lines.append(f"  {src} --> {dst}")

        lines.append("```")
        return "\n".join(lines)

    def _mermaid_sequence(self, data: Dict[str, Any]) -> str:
        """Generate a sequence diagram."""
        lines = ["```mermaid", "sequenceDiagram"]
        actors = data.get("actors", [])
        messages = data.get("messages", [])

        for actor in actors:
            lines.append(f"  participant {actor}")

        for msg in messages:
            src = msg.get("from", "")
            dst = msg.get("to", "")
            text = msg.get("text", "")
            msg_type = msg.get("type", "->")  # ->, -->, ->>, -->>
            lines.append(f"  {src} {msg_type} {dst}: {text}")

        lines.append("```")
        return "\n".join(lines)

    def _mermaid_class(self, data: Dict[str, Any]) -> str:
        """Generate a class diagram."""
        lines = ["```mermaid", "classDiagram"]
        classes = data.get("classes", [])
        relationships = data.get("relationships", [])

        for cls in classes:
            name = cls.get("name", "")
            lines.append(f"  class {name} {{")
            for attr in cls.get("attributes", []):
                lines.append(f"    +{attr}")
            for method in cls.get("methods", []):
                lines.append(f"    +{method}()")
            lines.append("  }")

        for rel in relationships:
            src = rel.get("from", "")
            dst = rel.get("to", "")
            rel_type = rel.get("type", "--|>")  # --|>, --*, --o, -->
            lines.append(f"  {src} {rel_type} {dst}")

        lines.append("```")
        return "\n".join(lines)

    def _mermaid_pie(self, data: Dict[str, Any]) -> str:
        """Generate a pie chart."""
        title = data.get("title", "Pie Chart")
        items = data.get("items", [])
        lines = ["```mermaid", "pie", f"  title {title}"]
        for item in items:
            lines.append(f"  \"{item.get('label', '')}\" : {item.get('value', 0)}")
        lines.append("```")
        return "\n".join(lines)

    def _mermaid_gantt(self, data: Dict[str, Any]) -> str:
        """Generate a Gantt chart."""
        title = data.get("title", "Gantt Chart")
        date_format = data.get("date_format", "YYYY-MM-DD")
        tasks = data.get("tasks", [])
        lines = ["```mermaid", "gantt", f"  title {title}", f"  dateFormat {date_format}"]
        for task in tasks:
            name = task.get("name", "")
            start = task.get("start", "")
            end = task.get("end", "")
            status = task.get("status", "")
            status_str = f"  {status}," if status else ""
            lines.append(f"  {name} :{status_str} {start}, {end}")
        lines.append("```")
        return "\n".join(lines)

    def _mermaid_timeline(self, data: Dict[str, Any]) -> str:
        """Generate a timeline."""
        title = data.get("title", "Timeline")
        events = data.get("events", [])
        lines = ["```mermaid", "timeline", f"  title {title}"]
        for event in events:
            date = event.get("date", "")
            desc = event.get("description", "")
            lines.append(f"  {date} : {desc}")
        lines.append("```")
        return "\n".join(lines)

    def _mermaid_mindmap(self, data: Dict[str, Any]) -> str:
        """Generate a mind map."""
        title = data.get("title", "Mind Map")
        level1 = data.get("children", [])

        def _render_mindmap(prefix: str, items: list, indent: str) -> list:
            result = []
            for item in items:
                if isinstance(item, str):
                    result.append(f"{indent}{prefix} {item}")
                elif isinstance(item, dict):
                    name = item.get("name", "")
                    children = item.get("children", [])
                    result.append(f"{indent}{prefix} {name}")
                    result.extend(_render_mindmap(prefix, children, indent + "    "))
            return result

        lines = ["```mermaid", "mindmap", f"  root({title})"]
        lines.extend(_render_mindmap("", level1, "  "))
        lines.append("```")
        return "\n".join(lines)

    # --------------------------------------------------- ASCII charts

    def generate_ascii_bar(
        self, data: Dict[str, str], title: str = "", width: int = 40,
    ) -> str:
        """Generate an ASCII bar chart."""
        if not data:
            return ""

        lines = []
        if title:
            lines.append(f"  {title}")
            lines.append("  " + "-" * (width + 10))

        max_label = max(len(k) for k in data.keys())
        max_value = max(data.values()) if data.values() else 1

        for label, value in data.items():
            bar_len = int(value / max_value * width) if max_value > 0 else 0
            bar = "█" * bar_len
            lines.append(f"  {label:<{max_label}} | {bar} {value}")

        return "\n".join(lines)

    def generate_ascii_line(
        self, data: List[float], title: str = "", height: int = 10, width: int = 40,
    ) -> str:
        """Generate an ASCII line chart."""
        if not data:
            return ""

        max_val = max(data) if data else 1
        min_val = min(data) if data else 0
        val_range = max_val - min_val or 1

        lines = []
        if title:
            lines.append(f"  {title}")

        for row in range(height, -1, -1):
            line = ""
            val_at_row = min_val + (row / height) * val_range
            for col in range(len(data)):
                scaled = (data[col] - min_val) / val_range * height
                if abs(scaled - row) < 0.5:
                    line += "●"
                elif scaled > row:
                    line += "│"
                else:
                    line += " "
            lines.append(f"  {line}")

        # X-axis
        lines.append("  " + "─" * len(data))
        return "\n".join(lines)

    # --------------------------------------------------- skill interface

    def get_skill_schema(self) -> Dict[str, Any]:
        return {
            "name": "chart",
            "description": "Generate charts and diagrams (Mermaid, ASCII, SVG)",
            "parameters": {
                "type": "object",
                "properties": {
                    "chart_type": {
                        "type": "string",
                        "enum": [
                            "flowchart", "sequence", "class", "pie", "gantt",
                            "timeline", "mindmap", "bar", "line",
                        ],
                        "description": "type of chart to generate",
                    },
                    "data": {
                        "type": "object",
                        "description": "chart data (nodes, edges, items, etc.)",
                    },
                    "title": {
                        "type": "string",
                        "description": "chart title",
                    },
                },
                "required": ["chart_type", "data"],
            },
        }

    async def run(self, args: Dict[str, Any]) -> str:
        """Execute chart generation."""
        chart_type = args.get("chart_type", "flowchart")
        data = args.get("data", {})
        title = args.get("title", "")

        if title:
            data["title"] = title

        mermaid_types = {"flowchart", "sequence", "class", "pie", "gantt", "timeline", "mindmap"}
        if chart_type in mermaid_types:
            return self.generate_mermaid(chart_type, data)
        elif chart_type == "bar":
            return self.generate_ascii_bar(data, title)
        elif chart_type == "line":
            return self.generate_ascii_line(data, title)
        return f"不支持的图表类型: {chart_type}"


# Singleton
_chart_generator: Optional[ChartGenerator] = None


def get_chart_generator() -> ChartGenerator:
    global _chart_generator
    if _chart_generator is None:
        _chart_generator = ChartGenerator()
    return _chart_generator