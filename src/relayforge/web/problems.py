from django.http import JsonResponse

from relayforge.core.security import ErrorCode, ProblemDetails

PROBLEMS = {
    400: (ErrorCode.VALIDATION, "VALIDATION", "درخواست نامعتبر است."),
    401: (ErrorCode.UNAUTHENTICATED, "AUTH_PERMISSION", "احراز هویت معتبر لازم است."),
    403: (ErrorCode.FORBIDDEN, "AUTH_PERMISSION", "دسترسی مجاز نیست."),
    404: (ErrorCode.NOT_FOUND, "VALIDATION", "مورد درخواستی یافت نشد."),
    405: (ErrorCode.VALIDATION, "VALIDATION", "روش درخواست مجاز نیست."),
    409: ("CONFLICT", "CONFLICT", "درخواست با وضعیت فعلی سازگار نیست."),
    422: (ErrorCode.VALIDATION, "VALIDATION", "ورودی درخواست نامعتبر است."),
    429: (ErrorCode.RATE_LIMITED, "QUOTA_RATE_LIMIT", "تعداد درخواست‌ها بیش از حد مجاز است."),
    500: (ErrorCode.INTERNAL, "INTERNAL", "خطای داخلی رخ داد."),
    503: (ErrorCode.UNAVAILABLE, "TRANSIENT", "سرویس موقتاً در دسترس نیست."),
}


class ProblemError(Exception):
    def __init__(self, status, code=None):
        self.status = status
        self.code = code


def problem(request, status, *, code=None, retry_after=None):
    default_code, category, message = PROBLEMS[status]
    details = ProblemDetails(
        code or default_code, category, message, request.correlation_id,
        suggested_action_fa="شناسهٔ مستأجر را وارد کنید." if code == "RF022" else None,
        retryable=status in {429, 503}, retry_after_sec=retry_after,
    )
    response = JsonResponse(details.payload(), status=status)
    if retry_after is not None:
        response["Retry-After"] = str(retry_after)
    return response
