# Physical validation before autonomous use

These checks remain pending on the actual instrument. The software simulator and regression tests validate orchestration and arithmetic; they do not certify liquid delivery or emergency stopping.

Use independently weighed collections with suitable balance resolution, measured fluid density at the test temperature, evaporation control, and repeated measurements. Collect at the **mixing-container inlet**, including the complete source-to-container route, rather than validating only displacement at the syringe outlet. Treat additions to a mixture separately from cleanup and priming deliveries.

| Check | Evidence to collect |
|---|---|
| Step conversion | Delivered mass for multiple commanded volumes and speeds; bias and repeatability |
| Minimum transfer | Repeated individual strokes around the provisional 0.05 mL floor; reject a floor that does not meet your required accuracy |
| Boundary partitioning | Volumes just below/above the common-line capacity and multiples of it; confirm the sum delivered across all strokes |
| Every source route | Source line dead volume, common line chase and clearance; verify liquid reaches the mixing container without residual liquid or air changing composition |
| Water distribution | Single and between-component water modes, including fixed fractions and several enabled stocks |
| Mixture recipe | Independent collection for each component and total mixture volume; verify concentrations using an appropriate independent analytical method |
| Stopped flow | Cell loading, inter-measurement displacement, repeat count, vessel reserve and cleanup |
| Continuous flow | Flow rate over measurement duration, syringe refills, cell approach, reserve and cleanup |
| Fluids | Intended solvents, viscosities and wetting properties, not only water |
| Stop/failure | Controlled interruption during each transfer stage; verify real stopping, checkpoint recovery and no unexpected continued motion |
| UI loss | Close/reopen during a stage; verify stage-boundary pause and actual-state reconnect |
| Engine/SDK loss | Controlled worker failure or application restart; verify no silent replay and correct recovery requirement |
| Contamination | Carryover blanks after maintenance, including ordered recovery → rinse → air empty |

Define acceptance limits for volume bias, variability, concentration, carryover and stopping time **before** evaluating results. The application does not supply experimentally justified limits.

Copy `delivery-template.csv` and record one independent collection per row. Use density in g/mL and tare/final mass in grams:

```sh
python scripts/validate_delivery.py measured-deliveries.csv --output delivery-summary.csv
```

The script groups by source, workflow, RPM and requested/commanded volume and reports mean delivered volume, bias against each volume basis, and coefficient of variation when at least two replicates exist. It never operates hardware or changes calibration. A single replicate cannot establish repeatability. Save the raw observations, apparatus configuration, fluid identity, temperature and acceptance decision with the instrument record.

Historical mixtures used requested recipe fractions in their nominal commanded-volume column. New runs distinguish rounded commands in stored context and component rows. Neither basis measures actual delivered volume; report gravimetric or other independent results separately.
