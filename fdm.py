"""FDM Graph Engine -- Dependency analysis with Tarjan's SCC and Kahn's topological sort.

Pure stdlib module. No external dependencies.
"""

from collections import defaultdict, deque


class DependencyGraph:
    """Directed dependency graph with cycle detection, topological sort,
    parallel group computation, and impact scoring."""

    def __init__(self):
        self._nodes: set[str] = set()
        self._edges: dict[str, list[str]] = defaultdict(list)          # node -> depends on
        self._reverse_edges: dict[str, list[str]] = defaultdict(list)  # node -> depended on by
        self._edge_order: list[tuple[str, str]] = []                   # insertion order

    # ── Mutators ──────────────────────────────────────────────────────

    def add_node(self, node_id: str) -> None:
        """Add a node (idempotent)."""
        self._nodes.add(node_id)

    def add_edge(self, from_id: str, to_id: str) -> None:
        """Declare that *from_id* depends on *to_id*. Auto-adds both nodes."""
        self._nodes.add(from_id)
        self._nodes.add(to_id)
        self._edges[from_id].append(to_id)
        self._reverse_edges[to_id].append(from_id)
        self._edge_order.append((from_id, to_id))

    # ── Tarjan's SCC (cycle detection) ────────────────────────────────

    def find_cycles(self) -> list[dict]:
        """Return strongly connected components of size > 1 using Tarjan's algorithm.

        Each result dict: {"nodes": [...], "weakest_edge": (from, to)}
        where weakest_edge is the most recently added edge participating in the cycle.
        """
        index_counter = [0]
        stack: list[str] = []
        on_stack: set[str] = set()
        index: dict[str, int] = {}
        lowlink: dict[str, int] = {}
        sccs: list[list[str]] = []

        def strongconnect(v: str) -> None:
            index[v] = lowlink[v] = index_counter[0]
            index_counter[0] += 1
            stack.append(v)
            on_stack.add(v)

            for w in self._edges.get(v, []):
                if w not in index:
                    strongconnect(w)
                    lowlink[v] = min(lowlink[v], lowlink[w])
                elif w in on_stack:
                    lowlink[v] = min(lowlink[v], index[w])

            if lowlink[v] == index[v]:
                scc: list[str] = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    scc.append(w)
                    if w == v:
                        break
                if len(scc) > 1:
                    sccs.append(scc)

        for node in sorted(self._nodes):
            if node not in index:
                strongconnect(node)

        results: list[dict] = []
        for scc in sccs:
            scc_set = set(scc)
            # Find weakest edge: most recently added edge within this SCC
            weakest = None
            for i in range(len(self._edge_order) - 1, -1, -1):
                fr, to = self._edge_order[i]
                if fr in scc_set and to in scc_set:
                    weakest = (fr, to)
                    break
            results.append({"nodes": sorted(scc), "weakest_edge": weakest})

        return results

    # ── Kahn's topological sort ───────────────────────────────────────

    def topological_sort(self) -> list[str]:
        """Return topological ordering via Kahn's algorithm.

        If graph has cycles, returns partial order of non-cyclic nodes
        (does NOT raise an error).
        """
        if not self._nodes:
            return []

        in_degree: dict[str, int] = {n: 0 for n in self._nodes}
        for node in self._nodes:
            for dep in self._edges.get(node, []):
                in_degree[node] = in_degree.get(node, 0)  # ensure exists
                # dep has an incoming edge from node perspective:
                # actually, edges[node] = deps means node depends on dep
                # so dep -> node in the dependency direction
                pass

        # Recompute: edge (from_id, to_id) means from_id depends on to_id.
        # Topological order: process to_id before from_id.
        # In-degree for topological sort = number of things that must come before.
        # from_id must come after to_id, so from_id has in-degree contribution from to_id.
        in_degree = {n: 0 for n in self._nodes}
        for node in self._nodes:
            for dep in self._edges.get(node, []):
                # node depends on dep => node must come after dep
                # => in the ordering graph, dep -> node, so node's in-degree += 1
                in_degree[node] += 1

        queue = deque(sorted(n for n in self._nodes if in_degree[n] == 0))
        result: list[str] = []

        while queue:
            node = queue.popleft()
            result.append(node)
            # For each node that depends on 'node', reduce its in-degree
            for dependent in self._reverse_edges.get(node, []):
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    # Insert in sorted position for deterministic output
                    inserted = False
                    for i, q_node in enumerate(queue):
                        if dependent < q_node:
                            queue.insert(i, dependent)
                            inserted = True
                            break
                    if not inserted:
                        queue.append(dependent)

        return result

    # ── Parallel groups ───────────────────────────────────────────────

    def compute_parallel_groups(self) -> list[list[str]]:
        """Group nodes by depth level for parallel execution.

        Group 0 = nodes with no dependencies (roots).
        Group N = nodes whose dependencies are all in groups < N.
        Cycle members placed in deepest group of their cycle members.
        """
        if not self._nodes:
            return []

        # Find cycle members
        cycle_sets: list[set[str]] = []
        for cyc in self.find_cycles():
            cycle_sets.append(set(cyc["nodes"]))

        def cycle_group_of(node: str) -> set[str] | None:
            for cs in cycle_sets:
                if node in cs:
                    return cs
            return None

        depth: dict[str, int] = {}

        # BFS from roots (Kahn-style level assignment)
        in_degree = {n: 0 for n in self._nodes}
        for node in self._nodes:
            for dep in self._edges.get(node, []):
                in_degree[node] += 1

        # Start with roots
        queue = deque()
        for n in sorted(self._nodes):
            if in_degree[n] == 0:
                depth[n] = 0
                queue.append(n)

        # Build lookup: node -> its cycle set (if any)
        node_cycle: dict[str, set[str]] = {}
        for cs in cycle_sets:
            for n in cs:
                node_cycle[n] = cs

        while queue:
            node = queue.popleft()
            for dependent in sorted(self._reverse_edges.get(node, [])):
                # Skip edges between members of the same cycle to prevent infinite loop
                node_cs = node_cycle.get(node)
                if node_cs is not None and dependent in node_cs:
                    continue
                new_depth = depth[node] + 1
                if dependent not in depth or new_depth > depth[dependent]:
                    depth[dependent] = new_depth
                    queue.append(dependent)

        # Handle nodes not reached (in cycles with no external root)
        for n in self._nodes:
            if n not in depth:
                depth[n] = 0

        # Unify cycle members to deepest level
        for cs in cycle_sets:
            max_depth = max(depth.get(n, 0) for n in cs)
            for n in cs:
                depth[n] = max_depth

        # Build groups
        max_d = max(depth.values()) if depth else 0
        groups: list[list[str]] = []
        for d in range(max_d + 1):
            group = sorted(n for n in self._nodes if depth[n] == d)
            if group:
                groups.append(group)

        return groups

    # ── Impact scores ─────────────────────────────────────────────────

    def compute_impact_scores(self) -> dict[str, int]:
        """For each node, count transitive downstream dependents.

        A downstream dependent of X is any node that transitively depends on X.
        Uses reverse_edges (node -> nodes that depend on it) + BFS.
        """
        scores: dict[str, int] = {}

        for node in self._nodes:
            # BFS from node along reverse edges to find all transitive dependents
            visited: set[str] = set()
            queue = deque(self._reverse_edges.get(node, []))
            for n in queue:
                visited.add(n)

            while queue:
                current = queue.popleft()
                for dependent in self._reverse_edges.get(current, []):
                    if dependent not in visited:
                        visited.add(dependent)
                        queue.append(dependent)

            scores[node] = len(visited)

        return scores

    # ── Bottleneck ────────────────────────────────────────────────────

    def find_bottleneck(self) -> str | None:
        """Return node with highest impact score. None if empty. Ties broken alphabetically."""
        if not self._nodes:
            return None

        scores = self.compute_impact_scores()
        if not scores:
            return None

        max_score = max(scores.values())
        # If all scores are 0 and there are nodes, return None
        if max_score == 0:
            return None
        candidates = [n for n, s in scores.items() if s == max_score]
        return sorted(candidates)[0]

    # ── Full analysis ─────────────────────────────────────────────────

    def analyze(self) -> dict:
        """Run all analyses and return combined results."""
        return {
            "cycles": self.find_cycles(),
            "topological_order": self.topological_sort(),
            "parallel_groups": self.compute_parallel_groups(),
            "critical_path": self.topological_sort(),
            "bottleneck": self.find_bottleneck(),
            "impact_scores": self.compute_impact_scores(),
        }
