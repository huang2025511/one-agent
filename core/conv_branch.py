"""Conversation Branching — tree-based conversation history with branching.

Provides:
  - ConversationTree: tree structure for conversation branches
  - Branch from any message: create alternative conversation paths
  - Switch between branches: navigate conversation history
  - Visualize branches: ASCII tree representation
  - Export branch: export a specific conversation branch
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ConversationNode:
    """A single node in the conversation tree."""
    node_id: str = ""
    parent_id: str = ""
    branch_id: str = ""  # which branch this node belongs to
    role: str = "user"  # user or assistant
    content: str = ""
    timestamp: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)
    children: List[str] = field(default_factory=list)  # child node IDs


class ConversationTree:
    """Tree-based conversation history with branching support.

    Allows users to:
    - Branch from any message: "what if I asked differently?"
    - Switch between branches: explore alternative conversation paths
    - Visualize the conversation tree
    - Export a specific branch
    """

    def __init__(self, session_id: str = ""):
        self._session_id = session_id
        self._nodes: Dict[str, ConversationNode] = {}
        self._branches: Dict[str, List[str]] = {}  # branch_id → [node_ids]
        self._active_branch = "main"
        self._active_node = ""
        self._node_counter = 0

        # Create root node
        self._create_node("", "system", "Conversation started", "main")

    def _create_node(
        self, parent_id: str, role: str, content: str, branch_id: str,
    ) -> str:
        """Create a new node and return its ID."""
        self._node_counter += 1
        node_id = f"node_{self._node_counter}"

        node = ConversationNode(
            node_id=node_id,
            parent_id=parent_id,
            branch_id=branch_id,
            role=role,
            content=content,
        )

        self._nodes[node_id] = node

        # Update parent's children
        if parent_id and parent_id in self._nodes:
            self._nodes[parent_id].children.append(node_id)

        # Track branch
        if branch_id not in self._branches:
            self._branches[branch_id] = []
        self._branches[branch_id].append(node_id)

        self._active_node = node_id
        return node_id

    def add_message(
        self, role: str, content: str, branch_id: Optional[str] = None,
    ) -> str:
        """Add a message to the conversation tree.

        Args:
            role: 'user' or 'assistant'
            content: message content
            branch_id: branch to add to (default: current active branch)
        """
        bid = branch_id or self._active_branch
        return self._create_node(self._active_node, role, content, bid)

    def branch_from(
        self, node_id: str, branch_name: str = "",
    ) -> str:
        """Create a new branch from an existing node.

        Returns the new branch ID.
        """
        if node_id not in self._nodes:
            raise ValueError(f"Node '{node_id}' not found")

        branch_id = branch_name or f"branch_{len(self._branches) + 1}"

        # Create a new branch starting point
        parent_id = self._nodes[node_id].parent_id
        self._active_node = parent_id if parent_id and parent_id in self._nodes else node_id
        self._active_branch = branch_id

        return branch_id

    def switch_branch(self, branch_id: str) -> bool:
        """Switch to a different branch."""
        if branch_id not in self._branches:
            return False
        self._active_branch = branch_id
        # Set active node to the last node in this branch
        nodes = self._branches[branch_id]
        if nodes:
            self._active_node = nodes[-1]
        return True

    def get_branch_path(self, branch_id: Optional[str] = None) -> List[ConversationNode]:
        """Get the full conversation path for a branch."""
        bid = branch_id or self._active_branch
        nodes = self._branches.get(bid, [])
        return [self._nodes[nid] for nid in nodes if nid in self._nodes]

    def get_conversation_history(
        self, branch_id: Optional[str] = None,
    ) -> List[Dict[str, str]]:
        """Get conversation history as a list of role/content dicts."""
        nodes = self.get_branch_path(branch_id)
        return [
            {"role": n.role, "content": n.content}
            for n in nodes
            if n.role in ("user", "assistant")
        ]

    def list_branches(self) -> List[Dict[str, Any]]:
        """List all branches with their stats."""
        branches = []
        for bid, node_ids in self._branches.items():
            nodes = [self._nodes[nid] for nid in node_ids if nid in self._nodes]
            if not nodes:
                continue
            branches.append({
                "id": bid,
                "name": bid,
                "messages": len(nodes),
                "first_message": nodes[0].content[:50] if nodes else "",
                "last_message": nodes[-1].content[:50] if nodes else "",
                "is_active": bid == self._active_branch,
                "created_at": nodes[0].timestamp if nodes else 0,
            })
        return branches

    # --------------------------------------------------- visualization

    def visualize(self, branch_id: Optional[str] = None) -> str:
        """Generate an ASCII tree representation of the conversation."""
        bid = branch_id or self._active_branch
        nodes = self.get_branch_path(bid)

        if not nodes:
            return "(empty conversation)"

        lines = [f"Conversation: {self._session_id} (branch: {bid})"]
        lines.append("─" * 40)

        for i, node in enumerate(nodes):
            role_icon = "👤" if node.role == "user" else "🤖"
            indent = "  " if i > 0 else ""
            preview = node.content[:60].replace("\n", " ")
            if len(node.content) > 60:
                preview += "..."
            lines.append(f"{indent}{role_icon} {preview}")

        return "\n".join(lines)

    def visualize_tree(self) -> str:
        """Generate a full tree visualization with all branches."""
        lines = [f"Conversation Tree: {self._session_id}"]
        lines.append("─" * 50)

        # Find root nodes (no parent)
        roots = [n for n in self._nodes.values() if not n.parent_id or n.parent_id not in self._nodes]

        for root in roots:
            lines.extend(self._render_node(root, "", True))

        # Show branches
        lines.append("")
        lines.append("Branches:")
        for branch in self.list_branches():
            marker = "→ " if branch["is_active"] else "  "
            lines.append(f"{marker}{branch['name']}: {branch['messages']} messages")

        return "\n".join(lines)

    def _render_node(
        self, node: ConversationNode, prefix: str, is_last: bool,
    ) -> List[str]:
        """Recursively render a node in the tree."""
        lines = []
        connector = "└─ " if is_last else "├─ "
        role_icon = "U" if node.role == "user" else "A" if node.role == "assistant" else "S"
        content = node.content[:40].replace("\n", " ")
        lines.append(f"{prefix}{connector}[{role_icon}] {content}")

        children = [self._nodes[cid] for cid in node.children if cid in self._nodes]
        for i, child in enumerate(children):
            child_prefix = prefix + ("   " if is_last else "│  ")
            child_is_last = i == len(children) - 1
            lines.extend(self._render_node(child, child_prefix, child_is_last))

        return lines

    # --------------------------------------------------- export

    def export_branch(
        self, branch_id: Optional[str] = None, format: str = "json",
    ) -> str:
        """Export a branch as JSON or Markdown."""
        import json

        nodes = self.get_branch_path(branch_id)

        if format == "json":
            data = {
                "session_id": self._session_id,
                "branch_id": branch_id or self._active_branch,
                "messages": [
                    {
                        "role": n.role,
                        "content": n.content,
                        "timestamp": n.timestamp,
                    }
                    for n in nodes
                ],
            }
            return json.dumps(data, ensure_ascii=False, indent=2)

        elif format == "markdown":
            lines = [f"# Conversation: {self._session_id}"]
            for n in nodes:
                role = "**User**" if n.role == "user" else "**Assistant**"
                lines.append(f"\n### {role}")
                lines.append(n.content)
            return "\n".join(lines)

        return ""

    # --------------------------------------------------- stats

    def get_stats(self) -> Dict[str, Any]:
        return {
            "session_id": self._session_id,
            "total_nodes": len(self._nodes),
            "total_branches": len(self._branches),
            "active_branch": self._active_branch,
            "branches": self.list_branches(),
        }


class ConversationBranchManager:
    """Manages conversation trees for multiple sessions."""

    def __init__(self):
        self._trees: Dict[str, ConversationTree] = {}

    def get_tree(self, session_id: str) -> ConversationTree:
        """Get or create a conversation tree for a session."""
        if session_id not in self._trees:
            self._trees[session_id] = ConversationTree(session_id)
        return self._trees[session_id]

    def add_message(
        self, session_id: str, role: str, content: str,
    ) -> str:
        """Add a message to a session's tree."""
        tree = self.get_tree(session_id)
        return tree.add_message(role, content)

    def branch(
        self, session_id: str, node_id: str, branch_name: str = "",
    ) -> str:
        """Create a branch in a session."""
        tree = self.get_tree(session_id)
        return tree.branch_from(node_id, branch_name)

    def switch_branch(self, session_id: str, branch_id: str) -> bool:
        """Switch active branch for a session."""
        tree = self.get_tree(session_id)
        return tree.switch_branch(branch_id)

    def get_history(
        self, session_id: str, branch_id: Optional[str] = None,
    ) -> List[Dict[str, str]]:
        """Get conversation history for a session."""
        tree = self.get_tree(session_id)
        return tree.get_conversation_history(branch_id)


# Singleton
_branch_manager: Optional[ConversationBranchManager] = None


def get_branch_manager() -> ConversationBranchManager:
    global _branch_manager
    if _branch_manager is None:
        _branch_manager = ConversationBranchManager()
    return _branch_manager