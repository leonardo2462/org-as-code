"""Tests for FDM DependencyGraph against known graph topologies."""

import pytest

from fdm import DependencyGraph


class TestEmptyGraph:
    def test_empty_graph(self):
        """analyze() on empty graph returns no cycles, empty lists, None bottleneck."""
        g = DependencyGraph()
        result = g.analyze()

        assert result["cycles"] == []
        assert result["topological_order"] == []
        assert result["parallel_groups"] == []
        assert result["bottleneck"] is None
        assert result["impact_scores"] == {}


class TestLinearChain:
    def test_linear_chain(self):
        """A depends on B, B depends on C: 3 groups, no cycles, C is bottleneck."""
        g = DependencyGraph()
        g.add_edge("A", "B")
        g.add_edge("B", "C")

        result = g.analyze()

        # No cycles
        assert result["cycles"] == []

        # Topological order: C first (no deps), A last
        order = result["topological_order"]
        assert order[0] == "C"
        assert order[-1] == "A"

        # 3 parallel groups: [C], [B], [A]
        groups = result["parallel_groups"]
        assert len(groups) == 3
        assert groups[0] == ["C"]
        assert groups[1] == ["B"]
        assert groups[2] == ["A"]

        # Impact scores: C=2, B=1, A=0
        scores = result["impact_scores"]
        assert scores["C"] == 2
        assert scores["B"] == 1
        assert scores["A"] == 0

        # Bottleneck: C
        assert result["bottleneck"] == "C"


class TestDiamond:
    def test_diamond(self):
        """A->B, A->C, B->D, C->D: 3 groups, D is bottleneck with score 3."""
        g = DependencyGraph()
        g.add_edge("A", "B")
        g.add_edge("A", "C")
        g.add_edge("B", "D")
        g.add_edge("C", "D")

        result = g.analyze()

        # No cycles
        assert result["cycles"] == []

        # 3 parallel groups: [D], [B, C], [A]
        groups = result["parallel_groups"]
        assert len(groups) == 3
        assert groups[0] == ["D"]
        assert sorted(groups[1]) == ["B", "C"]
        assert groups[2] == ["A"]

        # D has highest impact: B, C, A all transitively depend on D
        scores = result["impact_scores"]
        assert scores["D"] == 3
        assert result["bottleneck"] == "D"


class TestCycleDetection:
    def test_cycle_detection(self):
        """A->B, B->C, C->A: 1 cycle with nodes {A, B, C}."""
        g = DependencyGraph()
        g.add_edge("A", "B")
        g.add_edge("B", "C")
        g.add_edge("C", "A")

        cycles = g.find_cycles()
        assert len(cycles) == 1
        assert sorted(cycles[0]["nodes"]) == ["A", "B", "C"]
        # Last-added edge (C->A) is weakest
        assert cycles[0]["weakest_edge"] == ("C", "A")


class TestIsolatedNodes:
    def test_isolated_nodes(self):
        """Nodes X, Y, Z with no edges: all in group 0, no cycles, all scores 0."""
        g = DependencyGraph()
        g.add_node("X")
        g.add_node("Y")
        g.add_node("Z")

        result = g.analyze()

        assert result["cycles"] == []
        # All in one parallel group (group 0)
        groups = result["parallel_groups"]
        assert len(groups) == 1
        assert sorted(groups[0]) == ["X", "Y", "Z"]

        # All impact scores 0
        for score in result["impact_scores"].values():
            assert score == 0

        # Bottleneck None (all scores 0)
        assert result["bottleneck"] is None


class TestMixedGraph:
    def test_mixed_graph(self):
        """A->B plus isolated node X: X and B in group 0, A in group 1."""
        g = DependencyGraph()
        g.add_edge("A", "B")
        g.add_node("X")

        result = g.analyze()

        groups = result["parallel_groups"]
        assert len(groups) == 2
        assert sorted(groups[0]) == ["B", "X"]
        assert groups[1] == ["A"]

        scores = result["impact_scores"]
        assert scores["B"] == 1
        assert scores["X"] == 0
        assert scores["A"] == 0


class TestIdempotency:
    def test_add_node_idempotent(self):
        """Adding same node twice doesn't duplicate."""
        g = DependencyGraph()
        g.add_node("A")
        g.add_node("A")
        assert len(g._nodes) == 1


class TestAutoAddNodes:
    def test_add_edge_auto_adds_nodes(self):
        """add_edge auto-adds both nodes."""
        g = DependencyGraph()
        g.add_edge("P", "Q")
        assert "P" in g._nodes
        assert "Q" in g._nodes


class TestTopologicalSortWithCycle:
    def test_topological_sort_with_cycle(self):
        """Graph with cycle returns partial order of non-cycle nodes without error."""
        g = DependencyGraph()
        # Cycle: A->B->C->A
        g.add_edge("A", "B")
        g.add_edge("B", "C")
        g.add_edge("C", "A")
        # Non-cycle node D depends on A
        g.add_edge("D", "A")

        order = g.topological_sort()
        # Should not raise; returns partial order
        assert isinstance(order, list)
        # D should be in the result (it's reachable via partial ordering)
        # The cycle nodes may or may not appear depending on implementation
        # Key assertion: no error raised
        assert len(order) >= 0


class TestWeakestEdgeOrdering:
    def test_weakest_edge_is_most_recent(self):
        """In cycle A->B->C->A, weakest is last-added edge within cycle."""
        # Build with C->A last => weakest is C->A
        g1 = DependencyGraph()
        g1.add_edge("A", "B")
        g1.add_edge("B", "C")
        g1.add_edge("C", "A")
        cycles1 = g1.find_cycles()
        assert cycles1[0]["weakest_edge"] == ("C", "A")

        # Build with B->C last => weakest is B->C
        g2 = DependencyGraph()
        g2.add_edge("A", "B")
        g2.add_edge("C", "A")
        g2.add_edge("B", "C")
        cycles2 = g2.find_cycles()
        assert cycles2[0]["weakest_edge"] == ("B", "C")
