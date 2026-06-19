from django.urls import path

from simulator import views

urlpatterns = [
    path("", views.index, name="index"),
    path("linea/<str:route>/", views.line, name="line"),
    path("api/simulate/<str:route>/", views.api_simulate, name="api_simulate"),
]
