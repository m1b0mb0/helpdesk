from django.contrib import admin, messages
from django.utils.html import format_html, format_html_join

from .knowledge import article_source
from .models import KnowledgeArticle, SupportRequest
from .moderation import (
    InvalidDraft,
    InvalidModerationState,
    approve_request,
    reject_request,
)


@admin.register(SupportRequest)
class SupportRequestAdmin(admin.ModelAdmin):
    list_display = (
        "ticket_id", "event_id", "customer_email", "status", "created_at", "updated_at",
    )
    list_filter = ("status",)
    search_fields = ("event_id", "ticket_id", "customer_email")
    ordering = ("-created_at", "-pk")
    actions = ("approve_selected", "reject_selected")
    readonly_fields = tuple(field.name for field in SupportRequest._meta.fields) + ("source_links",)
    fieldsets = (
        ("Request", {"fields": (
            "event_id", "ticket_id", "customer_email", "status", "message",
            "created_at", "updated_at",
        )}),
        ("Draft", {"fields": ("draft_text", "source_links", "last_error")}),
        ("Moderation", {"fields": (
            "moderated_by", "moderated_at", "approved_text", "delivery_key",
        )}),
        ("Technical details", {"classes": ("collapse",), "fields": (
            "reply_url", "attempt_count", "dispatch_count", "processing_token",
            "lease_expires_at", "next_attempt_at", "draft_sources",
        )}),
    )

    @admin.display(description="Verified knowledge sources")
    def source_links(self, obj):
        """Never turn an arbitrary stored JSON URL into an operator hyperlink."""
        sources = obj.draft_sources
        if not isinstance(sources, list) or not sources:
            return "No verified sources."
        ids = [s["id"] for s in sources if isinstance(s, dict) and type(s.get("id")) is int]
        articles = KnowledgeArticle.objects.in_bulk(ids)
        verified = []
        seen = set()
        for source in sources:
            if not isinstance(source, dict) or type(source.get("id")) is not int:
                continue
            article = articles.get(source["id"])
            if article and source == article_source(article) and article.pk not in seen:
                verified.append(article_source(article))
                seen.add(article.pk)
        links = format_html_join(
            "", '<li><a href="{}">{}</a></li>',
            ((source["url"], source["title"]) for source in verified),
        )
        warning = "Some stored sources could not be verified." if len(verified) != len(sources) else ""
        return format_html("<ul>{}</ul>{}", links, warning)

    def has_moderate_permission(self, request):
        user = request.user
        return (
            user.is_active and user.is_staff
            and user.has_perm("support.view_supportrequest")
            and user.has_perm("support.moderate_supportrequest")
        )

    def _moderate_selected(self, request, queryset, service, decision):
        changed = skipped = 0
        # Each service re-reads and locks its row: list selection is never an
        # authorization to bypass state or draft validation, even on bulk POST.
        for request_id in queryset.values_list("pk", flat=True).iterator():
            try:
                service(request_id, actor=request.user)
            except (InvalidModerationState, InvalidDraft, SupportRequest.DoesNotExist):
                skipped += 1
            else:
                changed += 1
        self.message_user(
            request,
            f"{decision}: {changed}. Skipped (state changed or invalid draft): {skipped}.",
            level=messages.WARNING if skipped else messages.SUCCESS,
        )

    @admin.action(description="Approve selected drafts", permissions=["moderate"])
    def approve_selected(self, request, queryset):
        self._moderate_selected(request, queryset, approve_request, "Approved")

    @admin.action(description="Reject selected drafts", permissions=["moderate"])
    def reject_selected(self, request, queryset):
        self._moderate_selected(request, queryset, reject_request, "Rejected")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False
