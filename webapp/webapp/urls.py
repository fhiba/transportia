from django.urls import path

from simulator import views

urlpatterns = [
    path("", views.index, name="index"),
    path("ruta/", views.custom_route, name="custom_route"),
    path("linea/<str:route>/", views.line, name="line"),
    path("api/simulate/<str:route>/", views.api_simulate, name="api_simulate"),
    path("api/route/", views.api_route, name="api_route"),
]
