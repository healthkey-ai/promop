"""``/api/v1/phr/explanations/<kind>/<target>/`` — AI explanations of record items.

ONE generates them (its admins approve the prompts and choose the model) and
stores them here with the patient's token plus ``X-Explanation-Key``, a key
only ONE's server holds; PRomop never calls a model. Reading needs only the
patient's token, or a share token for items in the share.
"""
from __future__ import annotations

import hmac

from django.conf import settings
from django.utils import timezone
from rest_framework.response import Response

from patient_portal.models import AiExplanation

from .labs import labs
from .sharing import SharedRecordMixin, allowed, lab_group
from .views import PhrView

KINDS = dict(AiExplanation.KINDS)
TEXT_MAX, TARGET_MAX = 4000, 120
# Which shared section, and which item key in it, a target belongs to.
SECTIONS = {
    AiExplanation.DIAGNOSIS: 'diagnoses', AiExplanation.LAB_TEST: 'labs', AiExplanation.LAB_RESULT: 'labs',
    AiExplanation.GENETIC_TEST: 'genetics', AiExplanation.IMAGING_STUDY: 'imaging',
}


def explanation_json(row: AiExplanation, *, full: bool = True) -> dict:
    data = {'kind': row.kind, 'target': row.target_key, 'text': row.text, 'generated_at': row.generated_at.isoformat()}
    if full:
        data.update(prompt_version=row.prompt_version, model=row.model, source_hash=row.source_hash)
    return data


def _invalid_target(kind: str, target: str) -> Response | None:
    if kind not in KINDS:
        return Response({'detail': 'Unknown kind.'}, status=404)
    if not target or len(target) > TARGET_MAX:
        return Response({'detail': 'Unknown item.'}, status=404)
    return None


class ExplanationView(PhrView):
    http_method_names = ['get', 'put', 'options']

    def get(self, request, kind: str, target: str):
        if (bad := _invalid_target(kind, target)):
            return bad
        person, _record, error = self.resolve(request)
        if error:
            return error
        row = AiExplanation.objects.filter(person=person, kind=kind, target_key=target).first()
        if row is None:
            return Response({'detail': 'Not found.'}, status=404)
        return Response(explanation_json(row))

    def put(self, request, kind: str, target: str):
        if (bad := _invalid_target(kind, target)):
            return bad
        key = settings.PHR_EXPLANATION_WRITE_KEY
        sent = request.META.get('HTTP_X_EXPLANATION_KEY', '')
        if not key or not hmac.compare_digest(sent.encode(), key.encode()):
            return Response({'detail': 'Explanations are written by the app, not directly.'}, status=403)
        person, _record, error = self.resolve(request)
        if error:
            return error
        data = request.data
        text = str(data.get('text') or '').strip()
        fields = {k: str(data.get(k) or '').strip() for k in ('prompt_version', 'model', 'source_hash')}
        errors = {}
        if not text or len(text) > TEXT_MAX:
            errors['text'] = f'Between 1 and {TEXT_MAX} characters.'
        for name, limit in (('prompt_version', 64), ('model', 100), ('source_hash', 64)):
            if not fields[name] or len(fields[name]) > limit:
                errors[name] = f'Between 1 and {limit} characters.'
        if errors:
            return Response(errors, status=400)
        row, _created = AiExplanation.objects.update_or_create(
            person=person, kind=kind, target_key=target,
            defaults={'text': text, **fields, 'generated_at': timezone.now()},
        )
        return Response(explanation_json(row))


class SharedExplanationView(SharedRecordMixin, PhrView):
    """A stored explanation for an item the share includes; never generates one."""

    def get(self, request, kind: str, target: str):
        if (bad := _invalid_target(kind, target)):
            return bad
        person, record, error = self.resolve(request)
        if error:
            return error
        ok = allowed(self.share.selection, SECTIONS[kind])
        key = target
        if ok and SECTIONS[kind] == 'labs':
            test = next((t for t in labs(person, record)['tests'] if t['id'] == target), None)
            key = lab_group(test) if test else None
        if not ok or key is None or not ok(key):
            return Response({'detail': 'Not shared.'}, status=404)
        row = AiExplanation.objects.filter(person=person, kind=kind, target_key=target).first()
        if row is None:
            return Response({'detail': 'Not found.'}, status=404)
        return Response(explanation_json(row, full=False))
