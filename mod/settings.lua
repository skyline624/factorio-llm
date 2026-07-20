-- Reglages du mod factorio-llm (visibles dans Mod Settings -> Map).
-- Le RCON lui-meme (host/port/password) se configure au lancement de Factorio
-- via les flags --rcon-port / --rcon-password, pas ici.

data:extend({
  {
    type = "int-setting",
    name = "fl-tick-interval",
    order = "a",
    setting_type = "runtime-global",
    default_value = 60,
    minimum_value = 10,
    maximum_value = 1800,
    localised_name = {"", "factorio-llm: intervalle de tick (ticks entre deux pas d'execution)"},
  },
  {
    type = "int-setting",
    name = "fl-vision-radius",
    order = "b",
    setting_type = "runtime-global",
    default_value = 20,
    minimum_value = 5,
    maximum_value = 50,
    localised_name = {"", "factorio-llm: rayon de perception autour du personnage IA"},
  },
  {
    type = "bool-setting",
    name = "fl-debug-chat",
    order = "c",
    setting_type = "runtime-global",
    default_value = false,
    localised_name = {"", "factorio-llm: log de debug dans le chat serveur"},
  },
  {
    type = "bool-setting",
    name = "fl-test-mode",
    order = "d",
    setting_type = "runtime-global",
    default_value = false,
    localised_name = {"", "factorio-llm: mode test headless (pas de joueur, actions simulees)"},
    localised_description = {"", "Active le mode test headless : aucun joueur connecte requis, le mod spawn un character et utilise des actions instantanees/simulees. Desactive = production (joueur connecte, physique reelle). Toggle runtime via fl_ops.set_test_mode."},
  },
})