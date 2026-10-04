from django.urls import path

from . import views

app_name = "pi_durable"

urlpatterns = [
    path("stores", views.create_store, name="create-store"),
    path("stores/<uuid:store_id>/<str:method>", views.call, name="call"),
]
