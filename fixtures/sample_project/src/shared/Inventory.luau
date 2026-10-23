--!strict
-- Inventory: stores item counts per player.

local Inventory = {}
Inventory.__index = Inventory

export type Inventory = typeof(setmetatable({} :: { items: { [string]: number } }, Inventory))

function Inventory.new(): Inventory
	return setmetatable({ items = {} }, Inventory)
end

function Inventory.addItem(self: Inventory, itemId: string, amount: number)
	assert(amount > 0, "amount must be positive")
	self.items[itemId] = (self.items[itemId] or 0) + amount
end

function Inventory.count(self: Inventory, itemId: string): number
	return self.items[itemId] or 0
end

return Inventory
