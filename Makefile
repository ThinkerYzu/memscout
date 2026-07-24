# memscout developer tasks. The package is pure Python; there is no build step.

.PHONY: test install develop clean

test:                 ## run the unittest suite (fixtures + owned-child integration)
	./run-tests.sh

install:              ## install the `memscout` console script into the current env
	pip install .

develop:              ## editable install for development
	pip install -e .

clean:
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	rm -rf build dist *.egg-info
