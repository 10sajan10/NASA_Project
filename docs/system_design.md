# Variable-Centric Model Execution Design

This project is moving toward a model-agnostic dependency engine. The central
rule is:

```text
Everything is a producer of named cube variables.
```

Data access, model execution, and external file-format conversion are hidden
behind the same producer contract. The resolver only sees declared variables.

## Overall Flow

```mermaid
flowchart TD
    U[User requests model1_output] --> P[Planner / Resolver]
    P --> C0{model1_output satisfied in cube?}
    C0 -- yes --> O[Use cached cube variable]
    C0 -- no --> M1[Model 1 Adapter]

    M1 --> R1[Requires model2_output + data1_var]
    R1 --> C1{model2_output satisfied?}
    R1 --> C2{data1_var satisfied?}

    C1 -- no --> M2[Model 2 Adapter]
    C1 -- yes --> S2[Skip Model 2]
    C2 -- no --> D1[Data 1 Adapter]
    C2 -- yes --> S1[Skip Data 1]

    M2 --> R2[Requires data2_var]
    R2 --> C3{data2_var satisfied?}
    C3 -- no --> D2[Data 2 Adapter]
    C3 -- yes --> S3[Skip Data 2]

    D2 --> W2[Write data2_var to cube]
    W2 --> RM2[Run Model 2]
    S3 --> RM2
    RM2 --> WM2[Write model2_output to cube]

    D1 --> W1[Write data1_var to cube]
    W1 --> RM1[Run Model 1]
    S1 --> RM1
    WM2 --> RM1
    S2 --> RM1

    RM1 --> WM1[Write model1_output to cube]
    WM1 --> O
```

## Producer Contract

Every adapter should expose the same boundary:

```python
class MyProducer(ProducerV2):
    name = "my_producer"
    requires = (VarSpec("input_var", kind="static"),)
    produces = (VarSpec("output_var", kind="time"),)

    def run(self, cube, request):
        ...
        return {"output_var": 0}
```

The producer may be a data adapter, an in-process model, or a wrapper around an
external executable. The engine does not need to know which.

## Adapter Responsibilities

Data adapters:

```text
provider-specific search/access/reprojection -> canonical cube variables
```

Model adapters:

```text
canonical cube variables
  -> model-specific input format
  -> run model
  -> parse model-specific output
  -> canonical cube variables
```

The model-specific format can be NumPy arrays, GeoTIFF, NetCDF, CSV, WRF input
files, or anything else. That conversion belongs inside the adapter.

## Cube Reuse Rule

Before running a producer, the scheduler asks the cube whether the producer's
outputs are already satisfied:

```text
cube.satisfies(VarSpec(...), request)
```

The cube checks:

```text
variable exists
kind matches static/time
requested time window is covered
native resolution is acceptable, when declared
```

If all produced variables are satisfied, the producer is skipped. This is the
cache boundary: a model does not redownload or recompute inputs that are
already available in the cube.

## Migration Path

1. Keep existing drivers and model functions working.
2. Wrap old data drivers with `DataDriverAdapter`.
3. Wrap old model functions with `ModelFunctionAdapter`.
4. Migrate important models to direct `ProducerV2` subclasses as they evolve.
5. Keep the resolver and scheduler variable-centric.

The result is a scalable cascade system: adding a new model means registering
one producer that declares `requires` and `produces`, not editing the whole
pipeline.
