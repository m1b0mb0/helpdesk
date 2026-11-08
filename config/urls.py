"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path

from support.knowledge_views import knowledge_article, knowledge_index
from support.views import health, helpdesk_webhook

urlpatterns = [
    path('health', health, name='health'),
    path('webhooks/helpdesk', helpdesk_webhook, name='helpdesk_webhook'),
    path('knowledge/', knowledge_index, name='knowledge_index'),
    path('knowledge/<slug:slug>', knowledge_article, name='knowledge_article'),
    path('admin/', admin.site.urls),
]
