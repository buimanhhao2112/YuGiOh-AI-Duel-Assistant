# MCTS Combo Search

- Deck: Vanquish Soul x K9 (user-supplied list)
- Main Deck after filters: 40 cards
- Extra Deck after filters: 15 cards
- Starting hand: Vanquish Soul Heavy Borger, Triple Tactics Talent, K9-66a Jokul, K9-66b Lantern, K9-17 Izuna
- Target: highest modeled board value
- Random seed: 24
- MCTS simulations: 15000
- Expanded nodes: 301
- Banlist snapshot: loaded

> Prototype simulation. Validate any line with ocgcore before treating it as a legal duel sequence.
> End Phase and opponent responses are not simulated; Stake your Soul! summons are marked temporary.

## Generated sequence

**Bước 1:** Normal Summon [K9-17 Izuna]
**Bước 2:** Activate [K9-17 Izuna] on summon; send [K9-00 Lupis] from Deck to GY
**Bước 3:** Reveal [K9-66b Lantern]; Special Summon it and [K9-00 Lupis] from the GY
**Bước 4:** Activate [K9-66b Lantern]; add [A Case for K9] from Deck to hand
**Bước 5:** Xyz Summon [K9-17 "Ripper"] using [K9-17 Izuna, K9-66b Lantern]
**Bước 6:** Detach [K9-17 Izuna] from [K9-17 "Ripper"]; add [K9-04 Noroi] from Deck to hand
**Bước 7:** Reveal [K9-66a Jokul] and [K9-04 Noroi]; Special Summon both Level 5 monsters
**Bước 8:** Activate [K9-66a Jokul]; add non-Aqua [K9-17 Izuna] from Deck to hand

## End board

- K9-00 Lupis
- K9-17 "Ripper" — materials: K9-66b Lantern
- K9-66a Jokul
- K9-04 Noroi

Cards remaining in hand: Vanquish Soul Heavy Borger, Triple Tactics Talent, A Case for K9, K9-17 Izuna

## Board value

- Total score: 16.000
- Negates: 1
- Interruptions: 0
- Card advantage: 3
- Total ATK: 8800
- Deck resources used: 5
- FIRE/DARK/EARTH attributes retained in hand: 2/3
- Target reached: no

Generated at 2026-11-09T14:35:26+00:00.
