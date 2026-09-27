"""灶台相位切换与值守字段冻结业务规则。

冻结规则只有一处判定（:func:`is_run_frozen`），抽屉的只读展示、
保存拒绝、模型层兜底与瓦片/抽屉标记全部读它，避免多入口分叉。
"""
from decimal import Decimal

from django.core.exceptions import ValidationError

DRAWING_SOFT_POINT_MAX = Decimal("95")

# 进入「出胶」后，未收灶值守上被冻结的字段（探针不在此列）。
FROZEN_RUN_FIELDS = ("targetSoftPointC", "openedAt")
FROZEN_FIELD_LABELS = {
    "targetSoftPointC": "目标软化点",
    "openedAt": "开灶时间",
}
FROZEN_REJECT_MESSAGE = "灶已进入出胶，{fields}已冻结，须收灶回冷灶后方可修改。"


def is_run_frozen(run, hearth=None) -> bool:
    """
    唯一冻结判定：值守未收灶，且所属灶当前处于「出胶」相位。

    - 没有进行中的值守：不冻结；
    - 已收灶的历史值守：永不冻结；
    - 软化点探针（SoftPointProbe）不属于冻结范围，出胶期间仍可追加。
    """
    from apps.kiln.models import FireHearth

    if run is None or run.closedAt is not None:
        return False
    if hearth is None:
        hearth = run.hearth
    return hearth.phase == FireHearth.PHASE_DRAWING


def _normalize_form_value(name, value):
    """表单只精确到分钟，openedAt 比较时按分钟归一化，避免回显误判为改动。"""
    import datetime as _dt

    if name == "openedAt" and isinstance(value, _dt.datetime):
        return value.replace(second=0, microsecond=0)
    return value


def changed_frozen_field_labels(run, values) -> list:
    """对比 ``values``（如表单 cleaned_data）与值守现值，返回被改动的冻结字段中文名。"""
    changed = []
    for name in FROZEN_RUN_FIELDS:
        new_value = values.get(name)
        if new_value is None:
            continue
        current = _normalize_form_value(name, getattr(run, name))
        new_value = _normalize_form_value(name, new_value)
        if current != new_value:
            changed.append(FROZEN_FIELD_LABELS[name])
    return changed


def assert_frozen_fields_unchanged(run, values) -> None:
    """抽屉保存入口共用的拒绝逻辑：冻结态下改动冻结字段即中文拦下。"""
    if not is_run_frozen(run):
        return
    changed = changed_frozen_field_labels(run, values)
    if changed:
        raise ValidationError(FROZEN_REJECT_MESSAGE.format(fields="、".join(changed)))


def assert_frozen_save_allowed(run, update_fields=None) -> None:
    """
    模型层兜底：任何入口（视图、admin、直接 ORM）保存 CookRun 时，
    若处于冻结态且实际改动了冻结字段，一律拒绝。只改 closedAt 等
    非冻结字段（如收灶）不受影响。
    """
    if not is_run_frozen(run) or run.pk is None:
        return

    names = (
        FROZEN_RUN_FIELDS
        if update_fields is None
        else tuple(name for name in FROZEN_RUN_FIELDS if name in update_fields)
    )
    if not names:
        return

    from apps.kiln.models import CookRun

    old_values = CookRun.objects.filter(pk=run.pk).values(*names).first()
    if old_values is None:
        return
    changed = [
        FROZEN_FIELD_LABELS[name]
        for name in names
        if old_values[name] != getattr(run, name)
    ]
    if changed:
        raise ValidationError(FROZEN_REJECT_MESSAGE.format(fields="、".join(changed)))


def assert_can_enter_drawing(hearth) -> None:
    """
    进入「出胶」相位前：当前未收灶的 CookRun 须至少有一条
    softPointC <= 95 的 SoftPointProbe。
    """
    open_run = hearth.open_run()
    if open_run is None:
        raise ValidationError(
            {"phase": "无法进入出胶：该灶没有进行中的值守纪录。"}
        )

    ok = open_run.probes.filter(softPointC__lte=DRAWING_SOFT_POINT_MAX).exists()
    if not ok:
        raise ValidationError(
            {
                "phase": (
                    "无法进入出胶：进行中值守尚无软化点探针 "
                    f"≤ {DRAWING_SOFT_POINT_MAX}℃。"
                )
            }
        )


def change_hearth_phase(hearth, new_phase: str):
    """统一入口：改相位时校验出胶规则并保存；冻结态随相位由 is_run_frozen 派生。"""
    from apps.kiln.models import FireHearth

    if new_phase == FireHearth.PHASE_DRAWING:
        assert_can_enter_drawing(hearth)

    hearth.phase = new_phase
    hearth.save(update_fields=["phase"])
    return hearth
