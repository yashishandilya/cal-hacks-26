from protocol_gen import generate_dynamic_protocol
from validation import ValidationEngine

if __name__ == "__main__":
    print("🚀 RUNNING LIFECYCLE CHUNK 2 HARNESS SIMULATION...\n")
    
    # CASE A: Skincare Simulation
    skincareChat = (
        "I want to test my new prescription Retinol 1% (v_retinol_10). "
        "I know it cannot be used at the same time as my peeling acid (v_peeling_acid). "
        "If my clinical redness score spikes past an 8, throw an immediate barrier alert "
        "stating 'Skin barrier irritation threshold crossed! Prohibited active product combination.'"
    )
    
    print("Compiling Skincare Protocol via Gemini...")
    pathA = generate_dynamic_protocol(skincareChat, "exp_skin_999")
    print(f"✅ Protocol JSON saved smoothly to: {pathA}")
    
    skincareEngine = ValidationEngine(pathA)
    isValidSkin, skinMsg = skincareEngine.validateAction("v_retinol_10", ["v_peeling_acid"])
    print(f"-> Clash Evaluation Check: {isValidSkin} | {skinMsg}")
    isSkinSafe, skinCeilingMsg = skincareEngine.checkThreshold("clinical_redness_score", 9.0)
    print(f"-> Telemetry Metric Check: {isSkinSafe} | {skinCeilingMsg}\n")

    # CASE B: Pantry Waste Simulation
    pantryChat = (
        "I am doing a pantry optimization experiment with v_bananas and v_apples. "
        "They cause ethylene cross-contamination if stashed directly together. "
        "Set an alert rule on ambient_humidity_pct. If it goes greater than 65.0, trigger an exception "
        "stating 'Humidity threshold ceiling crossed! Spore propagation vector risks accelerated decay.'"
    )
    
    print("Compiling Pantry Optimization Protocol via Gemini...")
    pathB = generate_dynamic_protocol(pantryChat, "exp_pantry_777")
    print(f"✅ Protocol JSON saved smoothly to: {pathB}")
    
    pantryEngine = ValidationEngine(pathB)
    isValidPantry, pantryMsg = pantryEngine.validateAction("v_apples", ["v_bananas"])
    print(f"-> Clash Evaluation Check: {isValidPantry} | {pantryMsg}")
    isPantrySafe, pantryCeilingMsg = pantryEngine.checkThreshold("ambient_humidity_pct", 72.0)
    print(f"-> Telemetry Metric Check: {isPantrySafe} | {pantryCeilingMsg}\n")
    
    print("🏁 SEQUENTIAL LIFECYCLE COMPLIANCE COMPLETED SUCCESSFULLY.")