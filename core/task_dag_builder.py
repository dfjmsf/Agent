"""
TaskDagBuilder — 将 Manager 的任务草案归一化为确定性 DAG。

P0 目标：
- 用 target_file 作为稳定节点主键
- 统一重写 dependencies 与 task_id
- fail-fast 检测坏图
- 输出稳定拓扑顺序与最小 DAG 元数据
"""
from __future__ import annotations

import logging
import posixpath
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("TaskDagBuilder")


class TaskDagBuildError(ValueError):
    """DAG 构建失败：输入计划存在坏图或不可恢复歧义。"""


class TaskDagBuilder:
    """确定性 DAG 归一化器。"""

    def __init__(
        self,
        raw_tasks: List[Dict[str, Any]],
        project_spec: Optional[Dict[str, Any]] = None,
        module_groups: Optional[List[Dict[str, Any]]] = None,
        mode: str = "create",
    ):
        self.raw_tasks = raw_tasks or []
        self.project_spec = project_spec or {}
        self.module_groups = module_groups or []
        self.mode = mode

        self.nodes_by_key: Dict[str, Dict[str, Any]] = {}
        self.task_ref_to_node_key: Dict[str, str] = {}
        self.group_specs: Dict[str, Dict[str, Any]] = {}
        self.group_to_node_keys: Dict[str, set[str]] = defaultdict(set)
        self.edge_records: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self.incoming_by_node: Dict[str, set[str]] = defaultdict(set)
        self.outgoing_by_node: Dict[str, set[str]] = defaultdict(set)
        self.warnings: List[str] = []
        self.dropped_tasks: List[Dict[str, Any]] = []

    @classmethod
    def build_plan(
        cls,
        raw_tasks: List[Dict[str, Any]],
        project_spec: Optional[Dict[str, Any]] = None,
        module_groups: Optional[List[Dict[str, Any]]] = None,
        mode: str = "create",
    ) -> Dict[str, Any]:
        return cls(
            raw_tasks=raw_tasks,
            project_spec=project_spec,
            module_groups=module_groups,
            mode=mode,
        ).build()

    def build(self) -> Dict[str, Any]:
        self._ingest_module_groups()
        self._ingest_tasks()
        self._materialize_explicit_task_edges()
        self._materialize_module_group_edges()
        self._inject_ssr_template_edges()

        topo_order, ready_batches = self._topological_sort()
        final_tasks, task_id_map = self._materialize_tasks(topo_order, ready_batches)
        dag = self._build_dag_metadata(topo_order, ready_batches, task_id_map)

        logger.info(
            "🧭 DAG 构建完成: %s 节点 / %s 边 / %s ready batches / %s warnings",
            dag["node_count"],
            dag["edge_count"],
            len(ready_batches),
            len(self.warnings),
        )
        return {
            "tasks": final_tasks,
            "dag": dag,
        }

    def _ingest_module_groups(self) -> None:
        for index, raw_group in enumerate(self.module_groups, start=1):
            group_id = str(raw_group.get("group_id") or f"group_{index}")
            normalized_files: List[str] = []

            for raw_file in raw_group.get("files", []) or []:
                try:
                    normalized_files.append(self._normalize_target_file(raw_file))
                except TaskDagBuildError as exc:
                    self.warnings.append(f"模块组 {group_id} 含非法文件路径 {raw_file!r}: {exc}")

            self.group_specs[group_id] = {
                "group_id": group_id,
                "name": raw_group.get("name", ""),
                "description": raw_group.get("description", ""),
                "files": sorted(set(normalized_files)),
                "dependencies": list(dict.fromkeys(raw_group.get("dependencies", []) or [])),
            }

    def _ingest_tasks(self) -> None:
        for index, raw_task in enumerate(self.raw_tasks, start=1):
            raw_task = dict(raw_task or {})
            raw_task_id = str(raw_task.get("task_id") or f"raw_{index}")
            group_id = raw_task.get("_dag_group_id")

            try:
                target_file = self._normalize_target_file(raw_task.get("target_file"))
            except TaskDagBuildError as exc:
                self.dropped_tasks.append({
                    "task_id": raw_task_id,
                    "target_file": str(raw_task.get("target_file", "")),
                    "group_id": group_id,
                    "reason": f"bad_target_file: {exc}",
                })
                self.warnings.append(f"任务 {raw_task_id} 的 target_file 非法已跳过: {exc}")
                continue
            scoped_task_ref = self._make_scoped_task_ref(group_id, raw_task_id)
            existing_ref = self.task_ref_to_node_key.get(scoped_task_ref)
            if existing_ref and existing_ref != target_file:
                raise TaskDagBuildError(
                    f"检测到冲突 task_id 引用 {scoped_task_ref}: {existing_ref} vs {target_file}"
                )
            self.task_ref_to_node_key[scoped_task_ref] = target_file

            node = self.nodes_by_key.get(target_file)
            if node is None:
                node = {
                    "node_key": target_file,
                    "target_file": target_file,
                    "description": raw_task.get("description", ""),
                    "tech_stack": raw_task.get("tech_stack"),
                    "task_type": raw_task.get("task_type"),
                    "draft_action": raw_task.get("draft_action"),
                    "sub_tasks": raw_task.get("sub_tasks", []) or [],
                    "group_id": group_id,
                    "write_targets": raw_task.get("write_targets", [target_file]) or [target_file],
                    "_raw_dependencies": [],
                    "_source_order": index,
                }
                self.nodes_by_key[target_file] = node
            else:
                self.dropped_tasks.append({
                    "task_id": raw_task_id,
                    "target_file": target_file,
                    "group_id": group_id,
                    "reason": "duplicate_target_file",
                })
                self.warnings.append(f"重复 target_file 已合并: {target_file}")
                if not node.get("description") and raw_task.get("description"):
                    node["description"] = raw_task.get("description", "")
                if not node.get("tech_stack") and raw_task.get("tech_stack"):
                    node["tech_stack"] = raw_task.get("tech_stack")
                if not node.get("task_type") and raw_task.get("task_type"):
                    node["task_type"] = raw_task.get("task_type")
                if not node.get("draft_action") and raw_task.get("draft_action"):
                    node["draft_action"] = raw_task.get("draft_action")
                if not node.get("sub_tasks") and raw_task.get("sub_tasks"):
                    node["sub_tasks"] = raw_task.get("sub_tasks", []) or []
                if not node.get("group_id") and group_id:
                    node["group_id"] = group_id
                if not node.get("write_targets") and raw_task.get("write_targets"):
                    node["write_targets"] = raw_task.get("write_targets", []) or [target_file]

            node["_raw_dependencies"].extend(
                {
                    "dep_ref": str(dep),
                    "scope_group_id": group_id,
                }
                for dep in list(dict.fromkeys(raw_task.get("dependencies", []) or []))
                if dep
            )

            if group_id:
                self.group_to_node_keys[group_id].add(target_file)

        for group_id, spec in self.group_specs.items():
            for group_file in spec["files"]:
                if group_file in self.nodes_by_key:
                    self.group_to_node_keys[group_id].add(group_file)
                    if not self.nodes_by_key[group_file].get("group_id"):
                        self.nodes_by_key[group_file]["group_id"] = group_id

    def _materialize_explicit_task_edges(self) -> None:
        for node_key, node in self.nodes_by_key.items():
            for dep_info in node.get("_raw_dependencies", []):
                group_dep_nodes = self._resolve_group_dependency_nodes(
                    dep_info["dep_ref"],
                    current_node_key=node_key,
                    current_group_id=node.get("group_id"),
                )
                if group_dep_nodes is not None:
                    for dep_node_key in group_dep_nodes:
                        self._add_edge(
                            dep_node_key,
                            node_key,
                            reason=f"group_dependency_ref:{dep_info['dep_ref']}",
                            source_type="group_dependency_ref",
                        )
                    continue

                dep_node_key = self._resolve_dependency_ref(
                    dep_info["dep_ref"],
                    dep_info.get("scope_group_id"),
                )
                if not dep_node_key:
                    self.warnings.append(
                        f"任务 {node_key} 引用了不存在的依赖 {dep_info['dep_ref']}，已忽略"
                    )
                    continue
                self._add_edge(
                    dep_node_key,
                    node_key,
                    reason="llm_explicit_dependency",
                    source_type="task_dependency",
                )

    def _resolve_group_dependency_nodes(
        self,
        dep_ref: str,
        current_node_key: str,
        current_group_id: Optional[str],
    ) -> Optional[List[str]]:
        dep_ref = str(dep_ref or "").strip()
        if not dep_ref or dep_ref not in self.group_specs:
            return None

        if current_group_id and dep_ref == current_group_id:
            raise TaskDagBuildError(
                f"任务 {current_node_key} 使用所属模块组 {dep_ref} 作为依赖，"
                "这会导致自依赖歧义；应改为组内 task_id / target_file"
            )

        upstream_nodes = sorted(self.group_to_node_keys.get(dep_ref, set()), key=self._sort_key)
        if not upstream_nodes:
            self.warnings.append(f"任务 {current_node_key} 引用了空模块组依赖: {dep_ref}")
            return []

        return upstream_nodes

    def _materialize_module_group_edges(self) -> None:
        for group_id, group_spec in self.group_specs.items():
            downstream_nodes = sorted(self.group_to_node_keys.get(group_id, set()))
            if not downstream_nodes:
                continue

            for upstream_group in group_spec.get("dependencies", []) or []:
                upstream_nodes = sorted(self.group_to_node_keys.get(upstream_group, set()))
                if not upstream_nodes:
                    self.warnings.append(
                        f"模块组依赖未命中任务节点: {group_id} -> {upstream_group}"
                    )
                    continue

                for upstream_node in upstream_nodes:
                    for downstream_node in downstream_nodes:
                        if upstream_node == downstream_node:
                            continue
                        self._add_edge(
                            upstream_node,
                            downstream_node,
                            reason=f"module_group_dependency:{upstream_group}->{group_id}",
                            source_type="module_group_dependency",
                        )

    def _inject_ssr_template_edges(self) -> None:
        """确定性规则：templates/*.html 必须在 routes/views/app.py 之后执行。

        根因：Reviewer L0.6-C 检查 render_template() 传入的变量名是否与模板变量一致，
        但如果模板先于路由文件生成，sandbox 中没有 routes.py，L0.6-C 直接跳过 →
        模板使用了路由未传入的变量 → QA 阶段 Jinja2 UndefinedError。
        """
        ROUTE_BASENAMES = {'routes.py', 'views.py', 'app.py'}
        route_nodes = [
            key for key in self.nodes_by_key
            if key.replace("\\", "/").split("/")[-1].lower() in ROUTE_BASENAMES
        ]
        if not route_nodes:
            return

        injected = 0
        for node_key in self.nodes_by_key:
            if not node_key.endswith('.html'):
                continue
            for route_key in route_nodes:
                if route_key == node_key:
                    continue
                edge_key = (route_key, node_key)
                if edge_key not in self.edge_records:
                    self._add_edge(
                        route_key, node_key,
                        reason="ssr_template_depends_on_routes",
                        source_type="deterministic_ssr_rule",
                    )
                    injected += 1

        if injected:
            logger.info(f"🔗 [SSR 规则] 注入 {injected} 条模板→路由依赖边")

    def _topological_sort(self) -> Tuple[List[str], List[List[str]]]:
        indegree = {node_key: len(self.incoming_by_node.get(node_key, set())) for node_key in self.nodes_by_key}
        topo_order: List[str] = []
        ready_batches: List[List[str]] = []

        ready = sorted(
            [node_key for node_key, degree in indegree.items() if degree == 0],
            key=self._sort_key,
        )

        while ready:
            ready_batches.append(list(ready))
            next_ready = set()

            for node_key in ready:
                topo_order.append(node_key)
                for dependent in self.outgoing_by_node.get(node_key, set()):
                    indegree[dependent] -= 1
                    if indegree[dependent] == 0:
                        next_ready.add(dependent)

            ready = sorted(next_ready, key=self._sort_key)

        if len(topo_order) != len(self.nodes_by_key):
            remaining = sorted(node_key for node_key, degree in indegree.items() if degree > 0)
            raise TaskDagBuildError(f"检测到环依赖或不可完成 DAG: {remaining}")

        return topo_order, ready_batches

    def _materialize_tasks(
        self,
        topo_order: List[str],
        ready_batches: List[List[str]],
    ) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
        topo_index_map = {node_key: index for index, node_key in enumerate(topo_order)}
        ready_rank_map: Dict[str, int] = {}
        for batch_index, batch in enumerate(ready_batches):
            for node_key in batch:
                ready_rank_map[node_key] = batch_index

        task_id_map = {
            node_key: f"task_{index + 1}"
            for index, node_key in enumerate(topo_order)
        }

        final_tasks: List[Dict[str, Any]] = []
        for node_key in topo_order:
            node = self.nodes_by_key[node_key]
            dependencies = sorted(
                self.incoming_by_node.get(node_key, set()),
                key=lambda dep_node_key: topo_index_map[dep_node_key],
            )

            final_tasks.append({
                "task_id": task_id_map[node_key],
                "target_file": node["target_file"],
                "description": node.get("description", ""),
                "dependencies": [task_id_map[dep] for dep in dependencies],
                "tech_stack": node.get("tech_stack"),
                "task_type": node.get("task_type"),
                "draft_action": node.get("draft_action"),
                "sub_tasks": node.get("sub_tasks", []),
                "node_key": node_key,
                "group_id": node.get("group_id"),
                "topo_index": topo_index_map[node_key],
                "ready_rank": ready_rank_map.get(node_key, 0),
                "write_targets": node.get("write_targets", [node["target_file"]]),
            })

        return final_tasks, task_id_map

    def _build_dag_metadata(
        self,
        topo_order: List[str],
        ready_batches: List[List[str]],
        task_id_map: Dict[str, str],
    ) -> Dict[str, Any]:
        topo_index_map = {node_key: index for index, node_key in enumerate(topo_order)}
        edges = []
        for edge in sorted(self.edge_records.values(), key=lambda item: (topo_index_map[item["from_node_key"]], topo_index_map[item["to_node_key"]])):
            edges.append({
                "from_node_key": edge["from_node_key"],
                "to_node_key": edge["to_node_key"],
                "from_task_id": task_id_map[edge["from_node_key"]],
                "to_task_id": task_id_map[edge["to_node_key"]],
                "reason": " | ".join(edge["reasons"]),
                "source_type": ",".join(edge["source_types"]),
            })

        nodes = []
        for node_key in topo_order:
            node = self.nodes_by_key[node_key]
            nodes.append({
                "node_key": node_key,
                "task_id": task_id_map[node_key],
                "target_file": node["target_file"],
                "group_id": node.get("group_id"),
                "write_targets": node.get("write_targets", [node["target_file"]]),
            })

        return {
            "mode": self.mode,
            "node_count": len(topo_order),
            "edge_count": len(edges),
            "nodes": nodes,
            "edges": edges,
            "topo_order": [task_id_map[node_key] for node_key in topo_order],
            "topo_order_files": list(topo_order),
            "ready_batches": [
                [task_id_map[node_key] for node_key in batch]
                for batch in ready_batches
            ],
            "warnings": self.warnings,
            "dropped_tasks": self.dropped_tasks,
            "task_id_map": task_id_map,
        }

    def _resolve_dependency_ref(self, dep_ref: str, scope_group_id: Optional[str]) -> Optional[str]:
        dep_ref = str(dep_ref or "").strip()
        if not dep_ref:
            return None

        scoped_ref = self._make_scoped_task_ref(scope_group_id, dep_ref)
        if scope_group_id and scoped_ref in self.task_ref_to_node_key:
            return self.task_ref_to_node_key[scoped_ref]

        if dep_ref in self.task_ref_to_node_key:
            return self.task_ref_to_node_key[dep_ref]

        try:
            normalized = self._normalize_target_file(dep_ref)
        except TaskDagBuildError:
            return None

        if normalized in self.nodes_by_key:
            return normalized
        return None

    def _add_edge(self, from_node_key: str, to_node_key: str, reason: str, source_type: str) -> None:
        if from_node_key == to_node_key:
            raise TaskDagBuildError(f"检测到自依赖: {from_node_key}")

        edge_key = (from_node_key, to_node_key)
        edge = self.edge_records.get(edge_key)
        if edge is None:
            edge = {
                "from_node_key": from_node_key,
                "to_node_key": to_node_key,
                "reasons": [],
                "source_types": [],
            }
            self.edge_records[edge_key] = edge
            self.incoming_by_node[to_node_key].add(from_node_key)
            self.outgoing_by_node[from_node_key].add(to_node_key)

        if reason not in edge["reasons"]:
            edge["reasons"].append(reason)
        if source_type not in edge["source_types"]:
            edge["source_types"].append(source_type)

    def _sort_key(self, node_key: str) -> Tuple[int, int, str]:
        return (
            -len(self.outgoing_by_node.get(node_key, set())),
            self._node_category_priority(node_key),
            node_key,
        )

    @staticmethod
    def _make_scoped_task_ref(group_id: Optional[str], task_id: str) -> str:
        task_id = str(task_id or "").strip()
        if not task_id:
            return ""
        return f"{group_id}:{task_id}" if group_id else task_id

    @staticmethod
    def _normalize_target_file(target_file: Any) -> str:
        raw = str(target_file or "").strip().replace("\\", "/")
        if not raw:
            raise TaskDagBuildError("target_file 为空")

        normalized = posixpath.normpath(raw)
        if normalized in ("", ".", "/"):
            raise TaskDagBuildError(f"非法 target_file: {target_file!r}")
        if normalized.startswith("../"):
            raise TaskDagBuildError(f"target_file 越界: {target_file!r}")
        if normalized.startswith("./"):
            normalized = normalized[2:]
        if normalized.startswith("/"):
            normalized = normalized.lstrip("/")
        if not normalized or normalized == ".":
            raise TaskDagBuildError(f"非法 target_file: {target_file!r}")
        return normalized

    @staticmethod
    def _node_category_priority(target_file: str) -> int:
        path = target_file.lower()
        basename = path.rsplit("/", 1)[-1]

        if any(token in path for token in ("model", "schema", "entity", "migration", "db", "types")):
            return 0
        if any(token in path for token in ("service", "repository", "repo", "store", "util", "helper", "lib")):
            return 1
        if any(token in path for token in ("route", "api", "controller", "endpoint")):
            return 2
        if any(token in path for token in ("client", "request", "fetch", "http")):
            return 3
        if any(token in path for token in ("page", "view", "template", "component", "layout", "widget")):
            return 4
        if any(token in path for token in ("test", "spec", "doc")) or basename.startswith("readme"):
            return 5
        return 6
