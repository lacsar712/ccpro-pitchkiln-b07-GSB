from django import forms
from django.core.exceptions import ValidationError
from django.utils import timezone

from .models import CookRun, FireHearth, ResinLot, SoftPointProbe
from .services.floor_rules import (
    assert_can_enter_drawing,
    assert_frozen_fields_unchanged,
    is_run_frozen,
)


class ResinLotForm(forms.ModelForm):
    class Meta:
        model = ResinLot
        fields = ["lotCode", "originPlace", "arrivalKg", "receivedAt"]
        widgets = {
            "lotCode": forms.TextInput(attrs={"class": "field"}),
            "originPlace": forms.TextInput(attrs={"class": "field"}),
            "arrivalKg": forms.NumberInput(attrs={"class": "field", "step": "0.01"}),
            "receivedAt": forms.DateTimeInput(
                attrs={"class": "field", "type": "datetime-local"},
                format="%Y-%m-%dT%H:%M",
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["receivedAt"].input_formats = [
            "%Y-%m-%dT%H:%M",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
        ]
        if self.instance and self.instance.pk and self.instance.receivedAt:
            local = timezone.localtime(self.instance.receivedAt)
            self.initial["receivedAt"] = local.strftime("%Y-%m-%dT%H:%M")


class PhaseChangeForm(forms.Form):
    phase = forms.ChoiceField(
        label="相位",
        choices=FireHearth.PHASE_CHOICES,
        widget=forms.Select(attrs={"class": "field"}),
    )

    def __init__(self, *args, hearth=None, **kwargs):
        self.hearth = hearth
        super().__init__(*args, **kwargs)
        if hearth is not None and not self.is_bound:
            self.fields["phase"].initial = hearth.phase

    def clean_phase(self):
        phase = self.cleaned_data["phase"]
        if self.hearth is not None and phase == FireHearth.PHASE_DRAWING:
            assert_can_enter_drawing(self.hearth)
        return phase


class SoftPointProbeForm(forms.ModelForm):
    class Meta:
        model = SoftPointProbe
        fields = ["sampledAt", "softPointC", "samplerName"]
        widgets = {
            "sampledAt": forms.DateTimeInput(
                attrs={"class": "field", "type": "datetime-local"},
                format="%Y-%m-%dT%H:%M",
            ),
            "softPointC": forms.NumberInput(attrs={"class": "field", "step": "0.01"}),
            "samplerName": forms.TextInput(attrs={"class": "field"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["sampledAt"].input_formats = [
            "%Y-%m-%dT%H:%M",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
        ]
        if not self.is_bound and not (self.instance and self.instance.pk):
            self.initial["sampledAt"] = timezone.localtime().strftime("%Y-%m-%dT%H:%M")


class OpenCookRunForm(forms.ModelForm):
    class Meta:
        model = CookRun
        fields = ["resinLot", "openedAt", "targetSoftPointC"]
        widgets = {
            "resinLot": forms.Select(attrs={"class": "field"}),
            "openedAt": forms.DateTimeInput(
                attrs={"class": "field", "type": "datetime-local"},
                format="%Y-%m-%dT%H:%M",
            ),
            "targetSoftPointC": forms.NumberInput(
                attrs={"class": "field", "step": "0.01"}
            ),
        }

    def __init__(self, *args, hearth=None, **kwargs):
        self.hearth = hearth
        super().__init__(*args, **kwargs)
        self.fields["openedAt"].input_formats = [
            "%Y-%m-%dT%H:%M",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
        ]
        self.fields["resinLot"].queryset = ResinLot.objects.all()
        if not self.is_bound:
            self.initial["openedAt"] = timezone.localtime().strftime("%Y-%m-%dT%H:%M")

    def clean(self):
        cleaned = super().clean()
        if self.hearth is not None and self.hearth.open_run() is not None:
            raise forms.ValidationError("该灶已有进行中的值守，请先收灶再开新灶。")
        return cleaned


class CookRunForm(forms.ModelForm):
    """抽屉「保存值守」：仅可改开灶时间与目标软化点，冻结态同源拒绝。"""

    class Meta:
        model = CookRun
        fields = ["openedAt", "targetSoftPointC"]
        widgets = {
            "openedAt": forms.DateTimeInput(
                attrs={"class": "field", "type": "datetime-local"},
                format="%Y-%m-%dT%H:%M",
            ),
            "targetSoftPointC": forms.NumberInput(
                attrs={"class": "field", "step": "0.01"}
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["openedAt"].input_formats = [
            "%Y-%m-%dT%H:%M",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
        ]
        if self.instance and self.instance.pk:
            local = timezone.localtime(self.instance.openedAt)
            self.initial["openedAt"] = local.strftime("%Y-%m-%dT%H:%M")

    @property
    def frozen(self):
        return bool(self.instance and self.instance.pk and is_run_frozen(self.instance))

    def clean(self):
        cleaned = super().clean()
        if self.instance and self.instance.pk:
            try:
                assert_frozen_fields_unchanged(self.instance, cleaned)
            except ValidationError as exc:
                raise forms.ValidationError(str(exc.messages[0]))
        return cleaned

    def save(self, commit=True):
        if self.frozen and self.instance.pk:
            # 能走到保存说明表单层未发现改动；控件只精确到分钟，这里把冻结
            # 字段强制还原为库内现值，杜绝秒级截断经由 ORM 兜底被误拦。
            db_values = CookRun.objects.filter(pk=self.instance.pk).values(
                "openedAt", "targetSoftPointC"
            ).first()
            if db_values is not None:
                self.instance.openedAt = db_values["openedAt"]
                self.instance.targetSoftPointC = db_values["targetSoftPointC"]
        return super().save(commit=commit)


class AdminCookRunForm(forms.ModelForm):
    """admin 编辑值守也走同一个冻结判定，避免 save() 兜底直接抛 500。"""

    class Meta:
        model = CookRun
        fields = "__all__"

    def clean(self):
        cleaned = super().clean()
        if self.instance and self.instance.pk:
            try:
                assert_frozen_fields_unchanged(self.instance, cleaned)
            except ValidationError as exc:
                raise forms.ValidationError(str(exc.messages[0]))
        return cleaned

    def save(self, commit=True):
        # 与 CookRunForm 一致：冻结态提交未实际改动时还原库内冻结字段，
        # 避免控件精度差异在 CookRun.save() 兜底处抛错。
        if self.instance and self.instance.pk and is_run_frozen(self.instance):
            db_values = CookRun.objects.filter(pk=self.instance.pk).values(
                "openedAt", "targetSoftPointC"
            ).first()
            if db_values is not None:
                self.instance.openedAt = db_values["openedAt"]
                self.instance.targetSoftPointC = db_values["targetSoftPointC"]
        return super().save(commit=commit)
