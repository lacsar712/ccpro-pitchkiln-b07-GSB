from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.kiln.forms import CookRunForm
from apps.kiln.models import CookRun, FireHearth, ResinLot, SoftPointProbe
from apps.kiln.seed import ensure_seed_data
from apps.kiln.services.floor_rules import (
    change_hearth_phase,
    is_run_frozen,
)


def _fmt(dt):
    return timezone.localtime(dt).strftime("%Y-%m-%dT%H:%M")


class FreezeRuleTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("worker", password="x")
        self.client.force_login(self.user)
        self.lot = ResinLot.objects.create(
            lotCode="L-1",
            originPlace="松脂坳",
            arrivalKg=Decimal("100"),
            receivedAt=timezone.now(),
        )
        self.hearth = FireHearth.objects.create(
            lane=1, tag="灶-甲", resinGrade="特级", phase=FireHearth.PHASE_HOLDING
        )
        self.run = CookRun.objects.create(
            hearth=self.hearth,
            resinLot=self.lot,
            openedAt=timezone.now() - timezone.timedelta(hours=3),
            targetSoftPointC=Decimal("88.00"),
        )
        SoftPointProbe.objects.create(
            run=self.run,
            sampledAt=timezone.now() - timezone.timedelta(hours=1),
            softPointC=Decimal("93.00"),
            samplerName="阿萍",
        )

    def _enter_drawing(self):
        change_hearth_phase(self.hearth, FireHearth.PHASE_DRAWING)
        self.hearth.refresh_from_db()
        self.run.refresh_from_db()

    # —— 冻结判定 ——

    def test_not_frozen_before_drawing(self):
        self.assertFalse(is_run_frozen(self.run))
        self.assertFalse(self.run.is_frozen)

    def test_frozen_once_entering_drawing(self):
        self._enter_drawing()
        self.assertTrue(is_run_frozen(self.run))
        self.assertTrue(self.run.is_frozen)

    def test_closed_history_run_never_frozen(self):
        self._enter_drawing()
        old = CookRun.objects.create(
            hearth=self.hearth,
            resinLot=self.lot,
            openedAt=timezone.now() - timezone.timedelta(days=2),
            closedAt=timezone.now() - timezone.timedelta(days=1),
            targetSoftPointC=Decimal("80.00"),
        )
        self.assertFalse(is_run_frozen(old))
        # 历史行即使改目标也不拦
        old.targetSoftPointC = Decimal("70.00")
        old.save(update_fields=["targetSoftPointC"])

    # —— 入口一：抽屉保存（表单 / 视图）——

    def test_form_rejects_target_change_while_frozen(self):
        self._enter_drawing()
        form = CookRunForm(
            {"openedAt": _fmt(self.run.openedAt), "targetSoftPointC": "70.00"},
            instance=self.run,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("冻结", form.errors["__all__"][0])

    def test_form_allows_unchanged_submit_while_frozen(self):
        self._enter_drawing()
        form = CookRunForm(
            {
                "openedAt": _fmt(self.run.openedAt),
                "targetSoftPointC": str(self.run.targetSoftPointC),
            },
            instance=self.run,
        )
        self.assertTrue(form.is_valid(), form.errors.as_text())

    def test_edit_run_view_blocks_backend_tampering(self):
        """前端只读不够：绕过界面直接 POST 篡改，后端必须拒绝且不落库。"""
        self._enter_drawing()
        resp = self.client.post(
            f"/hearth/{self.hearth.pk}/run/",
            {
                "openedAt": _fmt(self.run.openedAt),
                "targetSoftPointC": "70.00",
            },
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp.status_code, 200)
        self.run.refresh_from_db()
        self.assertEqual(self.run.targetSoftPointC, Decimal("88.00"))
        self.assertContains(resp, "冻结中")  # 抽屉仍以只读态回画

    def test_edit_run_unchanged_submit_while_frozen_is_ok(self):
        """冻结态只读展示被绕过但值未改（控件只到分钟，库内带秒）：不得 500、不得改值。"""
        self._enter_drawing()
        original = CookRun.objects.get(pk=self.run.pk).openedAt
        resp = self.client.post(
            f"/hearth/{self.hearth.pk}/run/",
            {
                "openedAt": _fmt(original),
                "targetSoftPointC": str(self.run.targetSoftPointC),
            },
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(CookRun.objects.get(pk=self.run.pk).openedAt, original)
        self.assertEqual(
            CookRun.objects.get(pk=self.run.pk).targetSoftPointC, Decimal("88.00")
        )

    def test_edit_run_opened_at_also_frozen(self):
        self._enter_drawing()
        new_open = self.run.openedAt - timezone.timedelta(hours=2)
        resp = self.client.post(
            f"/hearth/{self.hearth.pk}/run/",
            {
                "openedAt": _fmt(new_open),
                "targetSoftPointC": str(self.run.targetSoftPointC),
            },
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp.status_code, 200)
        db_opened = CookRun.objects.get(pk=self.run.pk).openedAt
        self.assertEqual(db_opened, self.run.openedAt)

    # —— 入口二：直接 ORM / admin，也须被同一判定拦下 ——

    def test_model_save_blocks_frozen_fields(self):
        self._enter_drawing()
        self.run.targetSoftPointC = Decimal("70.00")
        with self.assertRaises(ValidationError):
            self.run.save()
        with self.assertRaises(ValidationError):
            self.run.save(update_fields=["targetSoftPointC"])

    def test_close_run_still_allowed_while_frozen(self):
        self._enter_drawing()
        # 收灶只改 closedAt，不应被冻结兜底误伤
        self.run.closedAt = timezone.now()
        self.run.save(update_fields=["closedAt"])

    # —— 探针不受冻结 ——

    def test_probe_still_addable_in_drawing(self):
        self._enter_drawing()
        before = self.run.probes.count()
        resp = self.client.post(
            f"/hearth/{self.hearth.pk}/probe/",
            {
                "sampledAt": _fmt(timezone.now()),
                "softPointC": "90.50",
                "samplerName": "阿坤",
            },
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.run.probes.count(), before + 1)

    # —— 回冷灶解冻，随后改目标成功 ——

    def test_unfreeze_after_phase_back_to_cold(self):
        self._enter_drawing()
        # 相位改回冷灶（未收灶的值守仍在）：冻结解除
        change_hearth_phase(self.hearth, FireHearth.PHASE_COLD)
        self.hearth.refresh_from_db()
        self.run.refresh_from_db()
        self.assertFalse(is_run_frozen(self.run))

        resp = self.client.post(
            f"/hearth/{self.hearth.pk}/run/",
            {"openedAt": _fmt(self.run.openedAt), "targetSoftPointC": "91.25"},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(CookRun.objects.get(pk=self.run.pk).targetSoftPointC, Decimal("91.25"))

    # —— 出胶准入回归 ——

    def test_enter_drawing_requires_qualifying_probe(self):
        self.run.probes.all().delete()
        SoftPointProbe.objects.create(
            run=self.run,
            sampledAt=timezone.now(),
            softPointC=Decimal("99.90"),
            samplerName="阿坤",
        )
        with self.assertRaises(ValidationError):
            change_hearth_phase(self.hearth, FireHearth.PHASE_DRAWING)
        self.hearth.refresh_from_db()
        self.assertEqual(self.hearth.phase, FireHearth.PHASE_HOLDING)


class BoardLegendAlignmentTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("worker", password="x")
        self.client.force_login(self.user)
        lot = ResinLot.objects.create(
            lotCode="L-1", originPlace="松脂坳", arrivalKg=Decimal("100"),
            receivedAt=timezone.now(),
        )
        now = timezone.now()

        def make(tag, lane, phase, open_run):
            h = FireHearth.objects.create(
                lane=lane, tag=tag, resinGrade="特级", phase=phase
            )
            if open_run:
                r = CookRun.objects.create(
                    hearth=h, resinLot=lot, openedAt=now,
                    targetSoftPointC=Decimal("88.00"),
                )
                SoftPointProbe.objects.create(
                    run=r, sampledAt=now, softPointC=Decimal("90.00"),
                    samplerName="阿萍",
                )
            return h

        self.d1 = make("灶-出1", 1, FireHearth.PHASE_DRAWING, True)
        self.d2 = make("灶-出2", 1, FireHearth.PHASE_DRAWING, True)
        make("灶-保", 1, FireHearth.PHASE_HOLDING, True)
        make("灶-冷", 2, FireHearth.PHASE_COLD, False)

    def test_drawing_tiles_match_legend(self):
        """出胶过滤瓦片数不得因冻结标记乱跳：瓦片 class 仍是 phase-drawing。"""
        from apps.kiln.views import _board_context

        ctx = _board_context()
        legend_drawing = dict(
            (key, count) for key, _label, count in ctx["phase_legend"]
        )[FireHearth.PHASE_DRAWING]
        drawing_hearths = FireHearth.objects.filter(
            phase=FireHearth.PHASE_DRAWING
        ).count()
        self.assertEqual(legend_drawing, drawing_hearths)
        self.assertEqual(legend_drawing, 2)
        self.assertEqual(
            ctx["frozen_hearth_ids"], {self.d1.pk, self.d2.pk}
        )

        resp = self.client.get("/floor/grid/")
        body = resp.content.decode()
        self.assertContains(resp, f'phase-{FireHearth.PHASE_DRAWING} is-frozen', count=2)
        # 出胶瓦片依旧带 phase-drawing（与图例同源），冻结只是附加标记
        self.assertEqual(body.count("tile-frozen"), 2)

    def test_frozen_drawer_is_readonly(self):
        resp = self.client.get(f"/hearth/{self.d1.pk}/drawer/", HTTP_HX_REQUEST="true")
        body = resp.content.decode()
        self.assertIn("冻结中", body)
        # 冻结态不渲染可提交的目标/开灶输入
        self.assertNotIn('name="targetSoftPointC"', body)
        self.assertNotIn('name="openedAt"', body)
        # 探针登记入口仍在
        self.assertIn('name="softPointC"', body)

    def test_unfrozen_drawer_has_save_form(self):
        h = FireHearth.objects.get(tag="灶-保")
        resp = self.client.get(f"/hearth/{h.pk}/drawer/", HTTP_HX_REQUEST="true")
        body = resp.content.decode()
        self.assertNotIn("冻结中", body)
        self.assertIn('name="targetSoftPointC"', body)
        self.assertIn(f'action="/hearth/{h.pk}/run/"', body)

    def test_full_board_page_renders(self):
        resp = self.client.get(f"/?hearth={self.d1.pk}")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "冻结中")
        self.assertContains(resp, "已收灶历史", 0)  # 本用例无历史值守

    def test_close_run_view_unfreezes_and_phase_cold(self):
        run = self.d1.runs.filter(closedAt__isnull=True).first()
        resp = self.client.post(
            f"/hearth/{self.d1.pk}/close-run/",
            {},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp.status_code, 200)
        self.d1.refresh_from_db()
        run.refresh_from_db()
        self.assertEqual(self.d1.phase, FireHearth.PHASE_COLD)
        self.assertFalse(is_run_frozen(run, self.d1))


class AdminFreezeFormTests(TestCase):
    def setUp(self):
        self.lot = ResinLot.objects.create(
            lotCode="L-9", originPlace="松脂坳", arrivalKg=Decimal("100"),
            receivedAt=timezone.now(),
        )
        self.hearth = FireHearth.objects.create(
            lane=1, tag="灶-管", resinGrade="特级", phase=FireHearth.PHASE_HOLDING
        )
        self.run = CookRun.objects.create(
            hearth=self.hearth, resinLot=self.lot,
            openedAt=timezone.now() - timezone.timedelta(hours=2),
            targetSoftPointC=Decimal("88.00"),
        )
        SoftPointProbe.objects.create(
            run=self.run, sampledAt=timezone.now(),
            softPointC=Decimal("90.00"), samplerName="阿萍",
        )
        change_hearth_phase(self.hearth, FireHearth.PHASE_DRAWING)
        self.hearth.refresh_from_db()
        self.run.refresh_from_db()

    def _admin_data(self, target="88.00"):
        return {
            "hearth": self.hearth.pk,
            "resinLot": self.lot.pk,
            "openedAt": timezone.localtime(self.run.openedAt).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
            "targetSoftPointC": target,
        }

    def test_admin_blocks_frozen_target_change(self):
        from apps.kiln.forms import AdminCookRunForm

        form = AdminCookRunForm(self._admin_data(target="70.00"), instance=self.run)
        self.assertFalse(form.is_valid())
        self.assertIn("冻结", form.errors["__all__"][0])

    def test_admin_unchanged_frozen_submit_saves(self):
        from apps.kiln.forms import AdminCookRunForm

        form = AdminCookRunForm(self._admin_data(), instance=self.run)
        self.assertTrue(form.is_valid(), form.errors.as_text())
        form.save()  # 不应触发模型兜底 500
        self.assertEqual(CookRun.objects.get(pk=self.run.pk).targetSoftPointC, Decimal("88.00"))


class SeedDataTests(TestCase):
    def test_drawing_hearth_has_open_run_and_closed_history(self):
        ensure_seed_data()
        h3 = FireHearth.objects.get(tag="坑火-西一")
        self.assertEqual(h3.phase, FireHearth.PHASE_DRAWING)

        open_run = h3.runs.filter(closedAt__isnull=True).first()
        self.assertIsNotNone(open_run)
        self.assertTrue(is_run_frozen(open_run, h3))

        closed = h3.runs.filter(closedAt__isnull=False)
        self.assertTrue(closed.exists())
        for r in closed:
            self.assertFalse(is_run_frozen(r, h3))
