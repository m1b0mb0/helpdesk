"""AI-authored sample documentation for the fictional MetricNest SaaS."""

ARTICLES = [
    {
        "slug": "pdf-export",
        "title": "Експорт звіту в PDF / Export a report to PDF",
        "keywords": ["pdf", "експорт", "export", "друк", "print", "завантаж"],
        "body": (
            "У MetricNest відкрийте звіт і виберіть Export → PDF. Для експорту потрібна роль Editor або Owner. "
            "PDF містить лише поточні фільтри та до 20 сторінок. Якщо кнопка неактивна, попросіть Owner надати роль Editor. "
            "Якщо файл порожній, скиньте фільтр дат і повторіть експорт. Для звіту понад 20 сторінок звузьте період або експортуйте CSV.\n\n"
            "Open the report and choose Export → PDF. An Editor or Owner role is required. "
            "PDF includes the current filters and at most 20 pages. Ask the Owner for Editor access if the button is disabled. "
            "For a blank file, reset the date filter and retry. Narrow the date range or use CSV for longer reports."
        ),
    },
    {
        "slug": "csv-export",
        "title": "CSV: кодування та роздільники / CSV encoding and delimiters",
        "keywords": ["csv", "excel", "кодуван", "encoding", "роздільник", "delimiter", "ієрогліф", "garbled", "експорт", "export"],
        "body": (
            "MetricNest створює CSV у UTF-8 з комами як роздільниками. Відкрийте Export → CSV у звіті. "
            "Якщо Excel показує спотворені українські літери, використайте Data → From Text/CSV і виберіть UTF-8 та Comma. "
            "CSV містить до 100 000 рядків; для більшого набору розбийте період фільтром дат.\n\n"
            "CSV files use UTF-8 and comma delimiters. Choose Export → CSV in the report. "
            "If Excel displays garbled characters, import with Data → From Text/CSV and select UTF-8 and Comma. "
            "Each export supports 100,000 rows; split larger datasets using the date filter."
        ),
    },
    {
        "slug": "data-refresh",
        "title": "Застарілі дані та синхронізація / Stale data and refresh",
        "keywords": ["синхрон", "sync", "онов", "refresh", "застар", "stale", "connector", "конектор", "підключен", "connection"],
        "body": (
            "Конектори MetricNest синхронізуються кожні 15 хвилин. Перевірте Connections → потрібний конектор → Last sync. "
            "Для ручної синхронізації Owner може натиснути Refresh now один раз на 5 хвилин. "
            "Статус Authorization expired означає, що треба натиснути Reconnect і повторно надати доступ. "
            "Не видаляйте конектор: це прибере налаштування зіставлення колонок.\n\n"
            "Connectors sync every 15 minutes. Check Connections → connector → Last sync. "
            "An Owner can choose Refresh now once every 5 minutes. For Authorization expired, choose Reconnect and authorize again. "
            "Do not delete the connector, because deletion removes its column mapping settings."
        ),
    },
    {
        "slug": "scheduled-email",
        "title": "Звіти за розкладом на email / Scheduled email reports",
        "keywords": ["розклад", "schedule", "email", "лист", "пошт", "спам", "spam", "достав", "delivery"],
        "body": (
            "У MetricNest Owner або Editor налаштовує Reports → Schedule: час, часовий пояс і підтверджені email-адреси. "
            "Планові листи доступні лише на тарифі Team. Якщо лист не прийшов, перевірте Schedule → Delivery log та папку Spam. "
            "Статус Unverified recipient означає, що одержувач має перейти за посиланням у листі підтвердження. "
            "Зміна розкладу діє з наступного запуску; минулі запуски автоматично не повторюються.\n\n"
            "Owners and Editors configure Reports → Schedule with a time, time zone, and verified email recipients. "
            "Scheduled emails require the Team plan. Check Schedule → Delivery log and Spam for missing messages. "
            "An Unverified recipient must follow the verification email link. Schedule changes apply to the next run; past runs are not replayed."
        ),
    },
    {
        "slug": "sharing-permissions",
        "title": "Спільний доступ і ролі / Sharing and permissions",
        "keywords": ["доступ", "access", "permission", "роль", "ролі", "role", "запрош", "invit", "sharing", "share", "колег", "colleague"],
        "body": (
            "MetricNest має ролі Owner, Editor та Viewer. Owner керує оплатою і командою; Editor редагує та експортує звіти; Viewer лише переглядає. "
            "Owner запрошує колег через Settings → Members. Запрошення дійсне 48 годин і прив'язане до email одержувача. "
            "Якщо доступу немає, увійдіть саме з цією адресою або попросіть Owner надіслати нове запрошення. "
            "Публічні анонімні посилання у MetricNest не підтримуються.\n\n"
            "Owner manages billing and members; Editor can edit and export; Viewer can only read reports. "
            "Owners invite colleagues from Settings → Members. Invitations expire after 48 hours and are bound to the recipient email. "
            "Sign in with that address or request a new invitation. Anonymous public report links are not supported."
        ),
    },
    {
        "slug": "timezone-filters",
        "title": "Часовий пояс і фільтри дат / Time zones and date filters",
        "keywords": ["часов", "timezone", "time zone", "дат", "date", "фільтр", "filter", "період", "period", "опівніч", "midnight"],
        "body": (
            "MetricNest зберігає часові мітки в UTC, а групує дні за Settings → Workspace → Time zone. "
            "Owner може змінити часовий пояс робочого простору. Фільтр Last 7 days включає сьогодні та попередні шість календарних днів у цьому поясі. "
            "Якщо підсумки різняться з джерелом, порівняйте часові пояси й однаковий діапазон дат. "
            "Зміна поясу не змінює вихідні дані, але може перенести події між сусідніми днями.\n\n"
            "Timestamps are stored in UTC; daily grouping uses Settings → Workspace → Time zone, editable by the Owner. "
            "Last 7 days includes today and the six preceding calendar days in that zone. "
            "Compare matching time zones and date ranges when totals differ. Changing the zone preserves source data but can move events between adjacent days."
        ),
    },
    {
        "slug": "plans-billing",
        "title": "Тарифи, ліміти й рахунки / Plans, limits, and invoices",
        "keywords": ["тариф", "plan", "ліміт", "limit", "оплат", "billing", "рахунк", "invoice", "підпис", "subscription", "картк", "payment"],
        "body": (
            "Тариф MetricNest Starter підтримує 3 конектори та 5 учасників; Team — 20 конекторів, 30 учасників і планові email-звіти. "
            "Owner бачить поточне використання та PDF-рахунки в Settings → Billing. Підвищення тарифу застосовується одразу. "
            "Зниження тарифу діє з наступного платіжного періоду після видалення зайвих конекторів та учасників. "
            "За відхиленого платежу оновіть спосіб оплати в Billing; повторне списання виконується наступного дня.\n\n"
            "Starter allows 3 connectors and 5 members; Team allows 20 connectors, 30 members, and scheduled emails. "
            "Owners find usage and PDF invoices in Settings → Billing. Upgrades apply immediately. "
            "Downgrades apply next billing period after excess connectors and members are removed. "
            "For a declined payment, update the payment method in Billing; collection retries the next day."
        ),
    },
    {
        "slug": "account-login",
        "title": "Вхід, пароль і двофакторна автентифікація / Login, passwords, and 2FA",
        "keywords": ["вхід", "увій", "login", "log in", "sign in", "парол", "password", "2fa", "автентиф", "authenticator", "двофактор", "recovery code"],
        "body": (
            "Для відновлення пароля MetricNest натисніть Forgot password на сторінці входу. Посилання скидання діє 30 хвилин; використовується лише найновіше. "
            "Якщо листа немає, перевірте Spam та правильність email. Двофакторна автентифікація вмикається в Profile → Security через застосунок-автентифікатор. "
            "Збережіть recovery codes офлайн. За втрати телефона використайте невикористаний recovery code. "
            "Якщо втрачено також коди, зверніться до підтримки для перевірки власника; пароль або коди надсилати не потрібно.\n\n"
            "Choose Forgot password on the sign-in page. Reset links expire after 30 minutes; only the newest link works. "
            "Check Spam and the email address if the message is missing. Enable authenticator-based 2FA under Profile → Security and save recovery codes offline. "
            "Use an unused recovery code if your phone is lost. If codes are also lost, contact support for ownership verification; never send passwords or codes."
        ),
    },
]
