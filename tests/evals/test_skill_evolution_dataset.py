"""Self-tests for the deterministic Skill-evolution evaluation dataset."""

from __future__ import annotations

import json
import runpy
import shutil
from types import SimpleNamespace

import pytest

from evals.skill_evolution.feedback import build_round_feedback
from evals.skill_evolution.reporting import render_report
from evals.skill_evolution.runner import (
    SkillEvaluationRunner,
    _copy_workspace,
    _managed_new_skill_names,
    _new_skill_names,
    _skill_snapshot,
    _validate_skill_isolation,
    classify_transfer,
    discover_tasks,
)
from evals.skill_evolution.verification import run_hidden_verifier


def _copy_stage(stage, tmp_path):
    target = tmp_path / stage.name
    shutil.copytree(stage.workspace, target)
    return target


def _oracle_answer(task_id, stage_name):
    if task_id == "code-modification-runtime-validation":
        return "## 修改内容\n完成边界校验。\n## 验证结果\n运行 pytest 和运行时行为测试。\n## 剩余风险\n无。"
    if task_id == "code-research-call-chain":
        if stage_name == "induction":
            return """## 入口与装配
preview_document 调用 RequestContext.from_headers 和 build_service，再进入 DocumentController。见 src/documents/api.py:6、src/documents/context.py:11、src/documents/container.py:16、src/documents/controller.py:8。

## 核心调用与分支
DocumentService.preview 先用 tenant_id、document_id 和 include_deleted 组成 key 调用 cache.get；未命中才调用 DocumentRepository.fetch 和 gateway.get，随后 cache.put。document_table 在装配仓储时进入，for_tenant 取得策略，renderer.render 接收 locale 和 redact。见 src/documents/service.py:11、src/documents/repository.py:9。

## 错误传播
仓储返回 None 时抛 DocumentMissing，控制器转为 HttpError 404；gateway.get 的 TimeoutError 先转为 StorageUnavailable，再由控制器转成 HttpError 503。见 src/documents/service.py:11、src/documents/repository.py:9、src/documents/controller.py:8。
"""
        return """## 入口与装配
publish_event 通过 PublishContext.from_headers 和 build_publisher 进入 PublishController，再调用 EventPublisher.publish。见 src/events/api.py:6、src/events/context.py:10、src/events/container.py:15、src/events/controller.py:8。

## 核心调用与分支
publisher 以 tenant_id 和 request_id 调用 receipts.get；命中时直接返回 duplicate，未命中则 router.resolve 后调用 handler，再 outbox.append 和 receipts.put。outbox_stream 在装配仓储时进入。见 src/events/publisher.py:10、src/events/repository.py:9。

## 错误传播
未知主题抛 UnknownTopic 并转为 HttpError 404；handler 可抛 InvalidPayload 并转为 422；gateway 的 ConnectionError 转为 OutboxUnavailable，再转为 HttpError 503。见 src/events/publisher.py:10、src/events/repository.py:9、src/events/controller.py:8。
"""
    if task_id == "project-build-service-contract":
        return "## 实现\n完成服务。\n## 验证\n执行接口行为测试。\n## 限制\n仅标准库。"
    return "报告已写入。"


def _solve_code_modification(stage, workspace):
    if stage.name == "induction":
        (workspace / "planning.py").write_text(
            """def resolve_batch_size(value, default):
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("batch_size must be an integer")
    if value < 1:
        raise ValueError("batch_size must be positive")
    return value
""",
            encoding="utf-8",
        )
    else:
        (workspace / "policy.py").write_text(
            """def resolve_timeout_ms(value, default):
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("timeout_ms must be an integer")
    if value < 1:
        raise ValueError("timeout_ms must be positive")
    return value
""",
            encoding="utf-8",
        )


def _solve_project_build(stage, workspace):
    if stage.name == "induction":
        (workspace / "notes_service.py").write_text(
            """class NotesService:
    def __init__(self):
        self._notes = {}

    @staticmethod
    def _text(value, name):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must not be empty")
        return value.strip()

    def create(self, note_id, title, body=""):
        note_id = self._text(note_id, "note_id")
        title = self._text(title, "title")
        if note_id in self._notes:
            raise ValueError("duplicate note")
        note = {"id": note_id, "title": title, "body": body}
        self._notes[note_id] = note
        return dict(note)

    def get(self, note_id):
        return dict(self._notes[note_id])

    def list(self):
        return [dict(note) for note in self._notes.values()]

    def delete(self, note_id):
        return dict(self._notes.pop(note_id))
""",
            encoding="utf-8",
        )
    else:
        (workspace / "inventory_service.py").write_text(
            """class InventoryService:
    def __init__(self):
        self._stock = {}

    @staticmethod
    def _sku(value):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("sku must not be empty")
        return value.strip()

    @staticmethod
    def _quantity(value):
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError("quantity must be an integer")
        if value < 1:
            raise ValueError("quantity must be positive")
        return value

    def add(self, sku, quantity):
        sku, quantity = self._sku(sku), self._quantity(quantity)
        self._stock[sku] = self._stock.get(sku, 0) + quantity
        return self._stock[sku]

    def available(self, sku):
        return self._stock[self._sku(sku)]

    def reserve(self, sku, quantity):
        sku, quantity = self._sku(sku), self._quantity(quantity)
        available = self._stock[sku]
        if quantity > available:
            raise ValueError("insufficient stock")
        self._stock[sku] = available - quantity
        return self._stock[sku]
""",
            encoding="utf-8",
        )


def _solve_web(stage, workspace):
    if stage.name == "induction":
        body = """# 结论

Use an exact dependency lock for dependency inputs. [S1]

Build the artifact in a clean isolated environment. [S2]

Store a cryptographic hash beside each artifact. [S3]

# 证据

The three recommendations map directly to S1, S2, and S3.

# 限制

The report is limited to the supplied source pack.
"""
    else:
        body = """# 结论

Emit structured logs with a request correlation identifier. [S1]

Track error rate and latency as service metrics. [S2]

Propagate trace context across service boundaries. [S3]

# 证据

The three recommendations map directly to S1, S2, and S3.

# 限制

The report is limited to the supplied source pack.
"""
    (workspace / "report.md").write_text(body, encoding="utf-8")


def test_dataset_has_four_isolated_task_pairs():
    tasks = discover_tasks()

    assert {task.category for task in tasks} == {
        "code_modification",
        "code_research",
        "project_build",
        "web_research",
    }
    for task in tasks:
        for stage in (task.induction, task.transfer):
            assert not stage.verifier.is_relative_to(stage.workspace)
            assert not list(stage.workspace.rglob("verifier.py"))
            assert not list(stage.workspace.rglob("task.json"))


def test_workspace_copy_removes_existing_codemate_state(tmp_path):
    source = tmp_path / "source"
    skill = source / ".codemate" / "skills" / "existing" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("existing", encoding="utf-8")
    (source / "app.py").write_text("value = 1\n", encoding="utf-8")
    cache = source / "__pycache__"
    cache.mkdir()
    (cache / "removed.cpython-312.pyc").write_bytes(b"stale")

    copied = _copy_workspace(source, tmp_path / "copied")

    assert (copied / "app.py").is_file()
    assert not (copied / ".codemate").exists()
    assert not (copied / "__pycache__").exists()


def test_new_skill_detection_uses_snapshot_name_difference(tmp_path):
    root = tmp_path / "workspace"
    existing = root / ".codemate" / "skills" / "existing" / "SKILL.md"
    existing.parent.mkdir(parents=True)
    existing.write_text("version one", encoding="utf-8")
    before = _skill_snapshot(root)

    existing.write_text("version two", encoding="utf-8")
    created = root / ".codemate" / "skills" / "created" / "SKILL.md"
    created.parent.mkdir(parents=True)
    created.write_text("new skill", encoding="utf-8")
    after = _skill_snapshot(root)

    assert before["existing"] != after["existing"]
    assert _new_skill_names(before, after) == ["created"]


def test_new_skill_detection_requires_evolution_ownership():
    class Store:
        @staticmethod
        def is_managed(name):
            return name == "managed"

    before = {"existing": "old"}
    after = {"existing": "new", "managed": "hash", "raw-write": "hash"}

    with pytest.raises(RuntimeError, match="raw-write"):
        _managed_new_skill_names(Store(), before, after)


def test_skill_isolation_requires_stage_local_roots_and_project_target(tmp_path):
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    agent = SimpleNamespace(
        paths=SimpleNamespace(
            home_root=home,
            user_skills=home / "skills",
            project_skills=workspace / ".codemate" / "skills",
        ),
        skill_evolution=SimpleNamespace(target="project"),
    )

    _validate_skill_isolation(agent, workspace, home)

    agent.paths.user_skills = tmp_path / "global-skills"
    with pytest.raises(RuntimeError, match="user Skills"):
        _validate_skill_isolation(agent, workspace, home)


def test_skill_isolation_rejects_user_evolution_target(tmp_path):
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    agent = SimpleNamespace(
        paths=SimpleNamespace(
            home_root=home,
            user_skills=home / "skills",
            project_skills=workspace / ".codemate" / "skills",
        ),
        skill_evolution=SimpleNamespace(target="user"),
    )

    with pytest.raises(RuntimeError, match="evolution target"):
        _validate_skill_isolation(agent, workspace, home)


def test_runner_disables_optional_maintenance_outside_skill_induction(tmp_path):
    args = SimpleNamespace(
        output_dir=tmp_path,
        provider="openai",
        model=None,
        base_url=None,
        max_steps=100,
        max_new_tokens=8192,
    )
    runner = SkillEvaluationRunner(args)

    induction = runner._agent_args(tmp_path, evolution=True)
    transfer = runner._agent_args(tmp_path, evolution=False)

    assert induction.benchmark is True
    assert induction.memory_backend == "disabled"
    assert induction.skill_evolution is True
    assert transfer.benchmark is True
    assert transfer.memory_backend == "disabled"
    assert transfer.skill_evolution is False


def test_runner_skips_transfer_when_induction_generates_no_skill(tmp_path):
    args = SimpleNamespace(output_dir=tmp_path)
    runner = SkillEvaluationRunner(args)
    task = discover_tasks()[0]
    runner._run_induction = lambda _task, _task_dir, _work_task_dir: {
        "workspace": str(tmp_path / "workspace"),
        "rounds": [],
        "terminal_feedback": "done",
        "generated_skills": [],
    }

    def unexpected_transfer(*_args, **_kwargs):
        raise AssertionError("transfer must not run without a generated Skill")

    runner._run_transfer_arm = unexpected_transfer

    result = runner.run_task(task)

    assert result["outcome"] == "no_skill_generated"
    assert result["transfer"] == {"baseline": None, "forced": None}
    report = render_report(result)
    assert "Transfer was skipped" in report
    assert runner.work_root.is_relative_to(tmp_path) is False


def test_each_verifier_matches_declared_check_ids():
    for task in discover_tasks():
        for stage in (task.induction, task.transfer):
            verify = runpy.run_path(str(stage.verifier))["verify"]
            raw = verify(stage.workspace, "", [])
            assert set(raw) == {check.id for check in stage.checks}


def test_initial_fixtures_do_not_pass_hard_completion():
    for task in discover_tasks():
        for stage in (task.induction, task.transfer):
            report = run_hidden_verifier(stage, stage.workspace, "", [])
            assert not report.completed


def test_reviewed_feedback_never_exposes_private_evidence():
    stage = discover_tasks()[0].induction
    report = run_hidden_verifier(stage, stage.workspace, "", [])
    feedback = build_round_feedback(report)

    assert "当前结果仍未完全满足要求" in feedback
    for check in report.checks:
        if check.evidence:
            assert check.evidence not in feedback


def test_oracle_solutions_pass_all_checks(tmp_path):
    for task in discover_tasks():
        for stage in (task.induction, task.transfer):
            workspace = _copy_stage(stage, tmp_path / task.id)
            if task.category == "code_modification":
                _solve_code_modification(stage, workspace)
            elif task.category == "project_build":
                _solve_project_build(stage, workspace)
            elif task.category == "web_research":
                _solve_web(stage, workspace)
            report = run_hidden_verifier(
                stage,
                workspace,
                _oracle_answer(task.id, stage.name),
                [],
            )
            assert report.completed, (task.id, stage.name, report.to_dict())
            assert report.total_score == 100.0, (
                task.id,
                stage.name,
                report.to_dict(),
            )


def test_web_research_verifier_accepts_chinese_source_backed_claims(tmp_path):
    task = next(item for item in discover_tasks() if item.category == "web_research")
    reports = {
        "induction": """## 结论

- 使用精确依赖锁。[S1]
- 在干净的隔离环境中构建。[S2]
- 为发布物保存密码学哈希。[S3]

## 证据

每项结论均引用对应资料。

## 限制

仅使用给定资料。
""",
        "transfer": """## 结论

- 结构化日志应包含请求关联标识。[S1]
- 跟踪错误率和延迟。[S2]
- 追踪上下文应跨越服务边界。[S3]

## 证据

每项结论均引用对应资料。

## 限制

仅使用给定资料。
""",
    }

    for stage in (task.induction, task.transfer):
        workspace = _copy_stage(stage, tmp_path / stage.name)
        (workspace / "report.md").write_text(reports[stage.name], encoding="utf-8")
        result = run_hidden_verifier(stage, workspace, "", [])

        assert result.completed, (stage.name, result.to_dict())
        assert result.total_score == 100.0


def test_web_research_verifier_requires_claim_local_citations(tmp_path):
    task = next(item for item in discover_tasks() if item.category == "web_research")
    workspace = _copy_stage(task.induction, tmp_path)
    (workspace / "report.md").write_text(
        """## 结论

- 使用精确依赖锁。[S2]
- 在干净的隔离环境中构建。[S3]
- 为发布物保存密码学哈希。[S1]

## 证据

引用存在但对应关系错误。[S1][S2][S3]

## 限制

仅使用给定资料。
""",
        encoding="utf-8",
    )

    result = run_hidden_verifier(task.induction, workspace, "", [])
    checks = {item.id: item for item in result.checks}

    assert checks["FUNC-001"].passed is False
    assert checks["QUALITY-001"].passed is False


def test_code_research_verifier_requires_cross_module_references(tmp_path):
    task = next(item for item in discover_tasks() if item.category == "code_research")
    workspace = _copy_stage(task.induction, tmp_path)
    answer = _oracle_answer(task.id, task.induction.name).replace(
        "src/documents/repository.py:9",
        "src/documents/repository.py:1",
    )

    result = run_hidden_verifier(task.induction, workspace, answer, [])
    checks = {item.id: item for item in result.checks}

    assert checks["FUNC-001"].passed is True
    assert checks["FUNC-002"].passed is True
    assert checks["FUNC-003"].passed is True
    assert checks["QUALITY-001"].passed is False


def test_transfer_classification_prioritizes_regression_over_score():
    baseline = {
        "verification": {
            "completed": True,
            "total_score": 80.0,
            "checks": [
                {"id": "A", "required": True, "passed": True},
                {"id": "B", "required": False, "passed": False},
            ],
        },
        "metrics": {"tool_steps": 4, "attempts": 5},
    }
    forced = json.loads(json.dumps(baseline))
    forced["verification"]["total_score"] = 90.0
    forced["verification"]["checks"][0]["passed"] = False
    forced["verification"]["checks"][1]["passed"] = True

    assert classify_transfer(baseline, forced) == "harmful"


def test_transfer_classification_marks_lower_optional_score_as_harmful():
    baseline = {
        "verification": {
            "completed": True,
            "total_score": 90.0,
            "checks": [{"id": "A", "required": False, "passed": True}],
        },
        "metrics": {"tool_steps": 4, "attempts": 5},
    }
    forced = json.loads(json.dumps(baseline))
    forced["verification"]["total_score"] = 80.0
    forced["verification"]["checks"][0]["passed"] = False

    assert classify_transfer(baseline, forced) == "harmful"
