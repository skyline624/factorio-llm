-- Utilitaires mathematiques pour factorio-llm (port de utils/math.ts d'airi).

local M = {}

-- Distance euclidienne entre deux positions {x, y}.
function M.distance(a, b)
  local dx = a.x - b.x
  local dy = a.y - b.y
  return math.sqrt(dx * dx + dy * dy)
end

-- Distance euclidienne au carre (pour des comparaisons sans sqrt).
function M.distance_sq(a, b)
  local dx = a.x - b.x
  local dy = a.y - b.y
  return dx * dx + dy * dy
end

return M