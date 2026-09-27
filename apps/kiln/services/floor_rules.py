"""灶台相位切换业务规则。"""
from decimal import Decimal

from django.core.exceptions import ValidationError

DRAWING_SOFT_POINT_MAX = Decimal("95")

RUN_FROZEN_MESSAGE = (
    "出胶中：目标软化点与开灶时刻已冻结，收灶或相位回冷灶后方可修改；"
    "探针可继续登记。"
)


def run_fields_frozen(hearth, run=None) -> bool:
    """
    「是否冻结」唯一判定：灶台处于出胶相位、且存在未收灶的值守时，
    该值守的目标软化点与开灶时刻冻结。已收灶的历史值守不受冻结。
    改相位与抽屉保存共用此判定，只读展示与保存拒绝同源。
    """
    from apps.kiln.models import FireHearth

    if run is None:
        run = hearth.open_run()
    return bool(
        run is not None
        and run.closedAt is None
        and hearth.phase == FireHearth.PHASE_DRAWING
    )


def assert_run_fields_mutable(hearth, run=None) -> None:
    """冻结中的值守拒绝修改目标软化点 / 开灶时刻（中文报错）。"""
    if run_fields_frozen(hearth, run):
        raise ValidationError(RUN_FROZEN_MESSAGE)


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
    """统一入口：改相位时校验出胶规则并保存。"""
    from apps.kiln.models import FireHearth

    if new_phase == FireHearth.PHASE_DRAWING:
        assert_can_enter_drawing(hearth)

    hearth.phase = new_phase
    hearth.save(update_fields=["phase"])
    return hearth
