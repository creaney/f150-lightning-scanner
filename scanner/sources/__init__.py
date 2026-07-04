# Source registry: populated as sources are extracted into their own modules.
# __main__.py reads REGISTRY to build --source choices and the sweep loop.
REGISTRY: dict = {}  # name -> sweep_function; filled by each sources/*.py on import

from scanner.sources.carmax import sweep_carmax
from scanner.sources.ebay import sweep_ebay
from scanner.sources.carvana import sweep_carvana
from scanner.sources.cargurus import sweep_cargurus
from scanner.sources.edmunds import sweep_edmunds, sweep_vroom
from scanner.sources.visor import sweep_visor
from scanner.sources.autotrader import sweep_autotrader
from scanner.sources.cars import sweep_cars_com

REGISTRY = {
    "carmax":     sweep_carmax,
    "ebay":       sweep_ebay,
    "carvana":    sweep_carvana,
    "cargurus":   sweep_cargurus,
    "edmunds":    sweep_edmunds,
    "vroom":      sweep_vroom,
    "visor":      sweep_visor,
    "autotrader": sweep_autotrader,
    "cars":       sweep_cars_com,
}
