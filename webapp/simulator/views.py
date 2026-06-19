from django.http import JsonResponse
from django.shortcuts import render

from . import engine


def index(request):
    return render(request, "simulator/index.html", {
        "lines": engine.list_available_lines(),
        "data_ready": engine.data_ready(),
        "load_error": engine.load_error(),
    })


def line(request, route):
    return render(request, "simulator/line.html", {"route": route})


def api_simulate(request, route):
    result = engine.simulate_route(route)
    status = 400 if "error" in result else 200
    return JsonResponse(result, status=status)
