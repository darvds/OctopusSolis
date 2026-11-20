# OctopusSolis

- Bump `VERSION` in `pyscript/apps/solis_octopus.py` with every change to the app (patch for fixes, minor for features), and add an entry to `CHANGELOG.md`. The owner checks the version on `sensor.solis_power_down` to see an update is installed.
- The app runs under pyscript's interpreter, not CPython: no generator expressions or `yield` (`tests/test_pyscript_compat.py` checks this).
- Run the tests with `python3 -m unittest discover -s tests -t .`
