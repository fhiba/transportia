from django.http import JsonResponse, HttpResponseBadRequest
from django.shortcuts import render
from django.views.decorators.http import require_GET

from . import engine


def index(request):
    return render(request, "simulator/index.html", {
        "lines": engine.list_available_lines(),
        "data_ready": engine.data_ready(),
        "load_error": engine.load_error(),
    })


def line(request, route):
    return render(request, "simulator/line.html", {"route": route})


def custom_route(request):
    """Página para trazar una ruta A→B entre dos puntos del mapa."""
    return render(request, "simulator/ruta.html")


def api_simulate(request, route):
    result = engine.simulate_route(route)
    status = 400 if "error" in result else 200
    return JsonResponse(result, status=status)


@require_GET
def api_route(request):
    """API: trazar ruta entre dos puntos. ?from=lat,lon&to=lat,lon."""
    try:
        from_raw = request.GET.get("from", "")
        to_raw = request.GET.get("to", "")
        a_lat, a_lon = (float(x) for x in from_raw.split(","))
        b_lat, b_lon = (float(x) for x in to_raw.split(","))
    except (ValueError, AttributeError):
        return JsonResponse({"error": "Formato inválido. Usá ?from=lat,lon&to=lat,lon"}, status=400)

    result = engine.route_between(a_lat, a_lon, b_lat, b_lon)
    status = 400 if "error" in result else 200
    return JsonResponse(result, status=status)
