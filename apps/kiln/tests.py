from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import CookRun, FireHearth, ResinLot, SoftPointProbe
from .seed import ensure_seed_data
from .services.floor_rules import (
    RUN_FROZEN_MESSAGE,
    assert_run_fields_mutable,
    run_fields_frozen,
)


class FreezeRuleTestCase(TestCase):
    """出胶冻结：判定函数、抽屉保存、相位回冷灶解冻。"""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            "worker", password="pw123456"
        )
        self.client.force_login(self.user)
        self.lot = ResinLot.objects.create(
            lotCode="脂-松脂坳-2601A",
            originPlace="松脂坳东沟",
            arrivalKg=Decimal("1000.00"),
            receivedAt=timezone.now() - timezone.timedelta(days=1),
        )
        self.hearth = FireHearth.objects.create(
            lane=1,
            tag="坳火-测",
            resinGrade="特级脂",
            phase=FireHearth.PHASE_HOLDING,
        )
        self.run = CookRun.objects.create(
            hearth=self.hearth,
            resinLot=self.lot,
            openedAt=timezone.now() - timezone.timedelta(hours=5),
            targetSoftPointC=Decimal("90.00"),
        )

    def _enter_drawing(self):
        self.hearth.phase = FireHearth.PHASE_DRAWING
        self.hearth.save(update_fields=["phase"])
        self.hearth.refresh_from_db()

    def _run_payload(self, **overrides):
        payload = {
            "targetSoftPointC": "91.50",
            "openedAt": "2026-09-20T08:30",
        }
        payload.update(overrides)
        return payload

    # —— 判定函数 ——

    def test_not_frozen_outside_drawing(self):
        self.assertFalse(run_fields_frozen(self.hearth))
        self.assertFalse(run_fields_frozen(self.hearth, self.run))

    def test_frozen_in_drawing_with_open_run(self):
        self._enter_drawing()
        self.assertTrue(run_fields_frozen(self.hearth))
        self.assertTrue(run_fields_frozen(self.hearth, self.run))

    def test_closed_history_run_not_frozen(self):
        self._enter_drawing()
        closed = CookRun.objects.create(
            hearth=self.hearth,
            resinLot=self.lot,
            openedAt=timezone.now() - timezone.timedelta(days=3),
            closedAt=timezone.now() - timezone.timedelta(days=2),
            targetSoftPointC=Decimal("84.00"),
        )
        self.assertFalse(run_fields_frozen(self.hearth, closed))
        # 未收灶值守仍冻结
        self.assertTrue(run_fields_frozen(self.hearth, self.run))

    def test_drawing_without_open_run_not_frozen(self):
        self.run.closedAt = timezone.now()
        self.run.save(update_fields=["closedAt"])
        self._enter_drawing()
        self.assertFalse(run_fields_frozen(self.hearth))

    def test_assert_mutable_raises_chinese_message(self):
        self._enter_drawing()
        with self.assertRaises(ValidationError) as ctx:
            assert_run_fields_mutable(self.hearth, self.run)
        self.assertIn(RUN_FROZEN_MESSAGE, ctx.exception.messages)

    # —— 抽屉保存入口 ——

    def test_update_run_blocked_when_frozen(self):
        self._enter_drawing()
        resp = self.client.post(
            reverse("update_run", args=[self.hearth.pk]),
            self._run_payload(),
            follow=True,
        )
        self.run.refresh_from_db()
        self.assertEqual(self.run.targetSoftPointC, Decimal("90.00"))
        self.assertContains(resp, "出胶中：目标软化点与开灶时刻已冻结")

    def test_update_run_blocked_when_frozen_htmx(self):
        self._enter_drawing()
        resp = self.client.post(
            reverse("update_run", args=[self.hearth.pk]),
            self._run_payload(),
            HTTP_HX_REQUEST="true",
        )
        self.run.refresh_from_db()
        self.assertEqual(self.run.targetSoftPointC, Decimal("90.00"))
        # htmx 重绘抽屉时中文提示随抽屉展示
        self.assertContains(resp, "出胶中：目标软化点与开灶时刻已冻结")
        self.assertContains(resp, "值守冻结")

    def test_update_run_allowed_when_not_frozen(self):
        resp = self.client.post(
            reverse("update_run", args=[self.hearth.pk]),
            self._run_payload(),
            follow=True,
        )
        self.run.refresh_from_db()
        self.assertEqual(self.run.targetSoftPointC, Decimal("91.50"))
        self.assertEqual(
            timezone.localtime(self.run.openedAt).strftime("%Y-%m-%d %H:%M"),
            "2026-09-20 08:30",
        )
        self.assertContains(resp, "值守参数已保存")

    def test_unfreeze_after_phase_back_to_cold(self):
        self._enter_drawing()
        # 相位回冷灶（值守仍在）
        self.client.post(
            reverse("change_phase", args=[self.hearth.pk]),
            {"phase": FireHearth.PHASE_COLD},
        )
        self.hearth.refresh_from_db()
        self.assertEqual(self.hearth.phase, FireHearth.PHASE_COLD)
        self.assertFalse(run_fields_frozen(self.hearth))
        # 随后改目标须成功
        self.client.post(
            reverse("update_run", args=[self.hearth.pk]),
            self._run_payload(),
        )
        self.run.refresh_from_db()
        self.assertEqual(self.run.targetSoftPointC, Decimal("91.50"))

    # —— 探针不受冻结影响 ——

    def test_probe_still_appendable_when_frozen(self):
        self._enter_drawing()
        resp = self.client.post(
            reverse("add_probe", args=[self.hearth.pk]),
            {
                "sampledAt": "2026-09-27T10:00",
                "softPointC": "94.50",
                "samplerName": "值守测试",
            },
            follow=True,
        )
        self.assertEqual(self.run.probes.count(), 1)
        self.assertContains(resp, "已登记探针 94.50℃")

    # —— 展示与判定同源 ——

    def test_drawer_marks_frozen_and_disables_fields(self):
        self._enter_drawing()
        resp = self.client.get(
            reverse("hearth_drawer", args=[self.hearth.pk]),
            HTTP_HX_REQUEST="true",
        )
        self.assertContains(resp, "值守冻结")
        self.assertContains(resp, 'disabled id="id_targetSoftPointC"')
        self.assertContains(resp, 'disabled id="id_openedAt"')
        self.assertNotContains(resp, "保存值守参数")

    def test_drawer_editable_when_not_frozen(self):
        resp = self.client.get(
            reverse("hearth_drawer", args=[self.hearth.pk]),
            HTTP_HX_REQUEST="true",
        )
        self.assertNotContains(resp, "值守冻结")
        self.assertContains(resp, "保存值守参数")


class BoardLegendTestCase(TestCase):
    """图例计数与瓦片一致；冻结瓦片有标记。"""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            "worker", password="pw123456"
        )
        self.client.force_login(self.user)
        lot = ResinLot.objects.create(
            lotCode="脂-桐油坑-2601B",
            originPlace="桐油坑北坡",
            arrivalKg=Decimal("800.00"),
            receivedAt=timezone.now(),
        )
        self.drawing = FireHearth.objects.create(
            lane=1, tag="坑火-甲", resinGrade="特级脂",
            phase=FireHearth.PHASE_DRAWING,
        )
        CookRun.objects.create(
            hearth=self.drawing,
            resinLot=lot,
            openedAt=timezone.now() - timezone.timedelta(hours=2),
            targetSoftPointC=Decimal("86.00"),
        )
        FireHearth.objects.create(
            lane=1, tag="坑火-乙", resinGrade="一级脂",
            phase=FireHearth.PHASE_COLD,
        )

    def test_legend_count_matches_drawing_tiles(self):
        resp = self.client.get(reverse("floor_grid"))
        html = resp.content.decode()
        self.assertIn("出胶 1", html)
        self.assertEqual(html.count("hearth-tile phase-drawing"), 1)
        self.assertEqual(html.count("hearth-tile phase-cold"), 1)

    def test_frozen_tile_marked(self):
        resp = self.client.get(reverse("floor_grid"))
        self.assertContains(resp, "冻结")

    def test_legend_updates_after_phase_change(self):
        # 误改相位被规则挡下：无 ≤95℃ 探针不能入出胶，图例与瓦片保持对齐
        cold = FireHearth.objects.get(tag="坑火-乙")
        self.client.post(
            reverse("change_phase", args=[cold.pk]),
            {"phase": FireHearth.PHASE_DRAWING},
        )
        resp = self.client.get(reverse("floor_grid"))
        html = resp.content.decode()
        self.assertIn("出胶 1", html)
        self.assertEqual(html.count("hearth-tile phase-drawing"), 1)


class SeedDataTestCase(TestCase):
    def test_drawing_hearth_has_open_and_closed_runs(self):
        ensure_seed_data()
        hearth = FireHearth.objects.get(tag="坑火-西一")
        self.assertEqual(hearth.phase, FireHearth.PHASE_DRAWING)
        open_run = hearth.open_run()
        self.assertIsNotNone(open_run)
        self.assertTrue(
            hearth.runs.filter(closedAt__isnull=False).exists(),
            "出胶灶须带一条已收灶历史",
        )
        # 未收灶值守冻结，已收灶历史不受冻
        self.assertTrue(run_fields_frozen(hearth, open_run))
        closed = hearth.runs.filter(closedAt__isnull=False).first()
        self.assertFalse(run_fields_frozen(hearth, closed))
