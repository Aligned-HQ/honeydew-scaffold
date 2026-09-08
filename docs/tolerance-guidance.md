# Tolerance guidance for scientific verifiers

Numerical tolerances depend on the data, units, observable, and scientific decision.
They require calibration for each task.

The table gives calibration questions rather than universal acceptance bounds.

| Quantity or outcome | Calibration basis | Scientific checks |
| --- | --- | --- |
| Numerical solution or residual | Discretization error, conditioning, and floating-point precision | Compare mesh refinements and defensible solvers. Relate residuals to errors in the requested observable. |
| Energy, mass, or flux | Measurement uncertainty and the conservation budget | Account for sources, sinks, and boundary fluxes. Use absolute bounds near zero. |
| Position, displacement, or angle | Instrument resolution and reference-frame uncertainty | Use consistent units and frames. Use circular differences for periodic angles. |
| Forecast or parameter estimate | Observational uncertainty and predictive performance | Assess reserved observations. Account for temporal and spatial dependence. |
| Engineering design or control | Feasibility margins and operating limits | Assess constraints across the specified loads, faults, and uncertainty. Distinguish hard limits from approximate objectives. |
| Optimization result | Feasibility tolerances and an objective bound | Accept alternative feasible solutions within the stated objective gap. |
| Formal proof | The exact proposition and trusted proof kernel | Require a complete proof of the stated result. Numerical agreement cannot establish a theorem. |

Record the calibration evidence in `solution/process.md` and
`metadata.verification_explanation`. State the acceptance criteria in
`instruction.md`. If relative errors become unstable near zero, use a justified
absolute tolerance. If valid solutions differ, assess their scientific properties
instead of requiring one reference representation.
