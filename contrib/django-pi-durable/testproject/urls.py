from django.urls import include, path

urlpatterns = [path("durable/", include("pi_durable.urls"))]
