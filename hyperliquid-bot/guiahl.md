# Guia de Integração — Hyperliquid para Trading Bots

## 1. Instalação e Setup Inicial

### 1.1 Instalar o SDK

```bash
npm install hyperliquid ethers
```

- **`hyperliquid`** — SDK oficial (v1.7.7+). Encapsula a API REST e assinatura de ordens.
- **`ethers`** — v6. Usado para derivar endereços e assinar ações L1.

### 1.2 Imports necessários

```typescript
import { Hyperliquid, signL1Action, floatToWire } from 'hyperliquid';
import { ethers } from 'ethers';
```

| Import | Uso |
|--------|-----|
| `Hyperliquid` | Classe principal do SDK (info + exchange) |
| `signL1Action` | Assinar ações raw para HIP-3 (multi-dex) |
| `floatToWire` | Converter float para string no formato wire da HL |

---

## 2. Conectando à API

### 2.1 Credenciais

Você precisa de duas coisas:
- **Private Key** — chave do signer (pode ser um "Agent Wallet" delegado)
- **Wallet Address** — endereço da carteira principal (que tem o USDC depositado)

```env
HL_PRIVATE_KEY=0xabc123...         # Chave privada do signer
HL_WALLET_ADDRESS=0xdef456...      # Endereço da carteira principal
```

**Dois modos de autenticação:**

| Modo | Descrição |
|------|-----------|
| **Direto** | `privateKey` e `walletAddress` pertencem à mesma carteira |
| **Agent** | `privateKey` é de um sub-wallet autorizado (Agent Setup na UI da HL). Mais seguro — a chave principal nunca sai da hardware wallet |

Para verificar qual modo:
```typescript
const signerAddress = ethers.computeAddress(privateKey);
const isAgentSetup = signerAddress.toLowerCase() !== walletAddress.toLowerCase();
```

### 2.2 Inicializar o SDK

```typescript
const sdk = new Hyperliquid({
  privateKey: '0xabc123...',
  walletAddress: '0xdef456...',
  enableWs: false,  // WebSocket desativado (usar polling)
});

// IMPORTANTE: chamar antes de qualquer operação
await sdk.ensureInitialized();
```

> **Nota:** `enableWs: false` é recomendado para bots. WebSocket da HL tem instabilidades — polling via REST é mais confiável.

### 2.3 Info API direta (sem SDK)

Para chamadas que o SDK não cobre (ex: HIP-3 dexes), use a API REST diretamente:

```typescript
async function hlInfo(type: string, extra: Record<string, unknown> = {}): Promise<unknown> {
  const resp = await fetch('https://api.hyperliquid.xyz/info', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ type, ...extra }),
  });
  if (!resp.ok) throw new Error(`HL info API ${type} failed: ${resp.status}`);
  return resp.json();
}
```

**Endpoints base:**
- **Info (leitura):** `https://api.hyperliquid.xyz/info` (POST)
- **Exchange (escrita):** `https://api.hyperliquid.xyz/exchange` (POST, requer assinatura)

---

## 3. Descobrir Mercados Disponíveis

### 3.1 Listar todos os ativos (perps)

```typescript
// Retorna [meta, contexts] — meta tem o universe de ativos, contexts tem preços/funding
const [meta, contexts] = await hlInfo('metaAndAssetCtxs') as [
  { universe: Array<{ name: string; szDecimals: number }> },
  Array<{ funding: string; midPx: string; markPx: string; openInterest: string }>,
];

// Listar todos os ativos
for (let i = 0; i < meta.universe.length; i++) {
  const asset = meta.universe[i];
  const ctx = contexts[i];
  console.log(`${asset.name}: midPx=${ctx.midPx}, funding=${ctx.funding}, szDecimals=${asset.szDecimals}`);
}
```

**Campos importantes de cada ativo:**

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `name` | string | Nome do ativo: "BTC", "ETH", "HYPE" |
| `szDecimals` | number | Casas decimais para tamanho da ordem (ex: 3 para BTC = 0.001 mínimo) |

**Campos de contexto (preço/mercado):**

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `midPx` | string | Preço mid (entre bid e ask) |
| `markPx` | string | Mark price (usado para liquidações) |
| `funding` | string | Funding rate atual (horário, ex: "0.0001" = 0.01%/h) |
| `openInterest` | string | Open interest total |

### 3.2 HIP-3 — Multi-DEX (ações, commodities, etc.)

A Hyperliquid tem múltiplos DEXes de perpétuos:

| DEX | Prefixo | Exemplos |
|-----|---------|----------|
| Default | (nenhum) | BTC, ETH, SOL, HYPE |
| xyz (Wagyu) | `xyz:` | xyz:AMZN, xyz:AAPL, xyz:TSLA |
| cash | `cash:` | cash:NVDA |

```typescript
// Listar os DEXes disponíveis
const perpDexList = await hlInfo('perpDexs') as Array<{ name: string } | null>;
// Retorna: [null, { name: "xyz" }, { name: "cash" }]
// null = DEX padrão (crypto)

// Buscar ativos de um DEX específico (ex: xyz)
const [xyzMeta, xyzCtxs] = await hlInfo('metaAndAssetCtxs', { dex: 'xyz' }) as [
  { universe: Array<{ name: string; szDecimals: number }> },
  Array<{ funding: string; midPx: string }>,
];

for (let i = 0; i < xyzMeta.universe.length; i++) {
  console.log(`xyz:${xyzMeta.universe[i].name}: midPx=${xyzCtxs[i].midPx}`);
}
```

### 3.3 Verificar se um símbolo existe

```typescript
async function resolveSymbol(symbol: string): Promise<string | null> {
  // 1. Tentar no DEX padrão
  const [meta] = await hlInfo('metaAndAssetCtxs') as [{ universe: Array<{ name: string }> }, unknown[]];
  if (meta.universe.some(a => a.name === symbol)) return symbol;

  // 2. Tentar com prefixos HIP-3
  for (const dex of ['xyz', 'cash']) {
    const [dexMeta] = await hlInfo('metaAndAssetCtxs', { dex }) as [{ universe: Array<{ name: string }> }, unknown[]];
    if (dexMeta.universe.some(a => a.name === symbol)) {
      return `${dex}:${symbol}`; // ex: "AMZN" → "xyz:AMZN"
    }
  }

  return null; // Não encontrado em nenhum DEX
}
```

### 3.4 Obter todos os preços mid

```typescript
// Apenas para ativos do DEX padrão (crypto)
await sdk.ensureInitialized();
const allMids = await sdk.info.getAllMids(true) as Record<string, string>;
// { "BTC": "67543.2", "ETH": "3421.1", "SOL": "178.5", ... }

console.log(`BTC mid: $${allMids['BTC']}`);
```

Para ativos HIP-3, use `metaAndAssetCtxs` com o parâmetro `dex` (ver seção 3.2).

---

## 4. Consultar Posições

### 4.1 Estado da Clearinghouse (posições abertas)

```typescript
// DEX padrão (crypto)
await sdk.ensureInitialized();
const state = await sdk.info.perpetuals.getClearinghouseState(walletAddress, true);

for (const ap of state.assetPositions) {
  const pos = ap.position;
  console.log({
    coin: pos.coin,
    size: parseFloat(String(pos.szi)),           // Negativo = short
    notional: parseFloat(String(pos.positionValue)),
    entryPrice: parseFloat(String(pos.entryPx)),
    unrealizedPnl: parseFloat(String(pos.unrealizedPnl)),
    leverage: pos.leverage,
    liquidationPx: pos.liquidationPx,
  });
}
```

```typescript
// HIP-3 (ex: xyz dex)
const xyzState = await hlInfo('clearinghouseState', {
  user: walletAddress,
  dex: 'xyz',
}) as { assetPositions: Array<{ position: Record<string, unknown> }> };
```

### 4.2 Buscar posição específica

```typescript
function getPosition(state: any, coin: string) {
  const ap = state.assetPositions.find(
    (ap: any) => String(ap.position.coin) === coin
  );
  if (!ap) return { size: 0, side: 'none' };

  const szi = parseFloat(String(ap.position.szi));
  return {
    size: Math.abs(szi),
    side: szi < 0 ? 'short' : szi > 0 ? 'long' : 'none',
    entryPrice: parseFloat(String(ap.position.entryPx)),
    unrealizedPnl: parseFloat(String(ap.position.unrealizedPnl)),
    notionalUsd: Math.abs(parseFloat(String(ap.position.positionValue))),
  };
}
```

### 4.3 Saldo da conta (equity USDC)

```typescript
await sdk.ensureInitialized();
const spotState = await sdk.info.spot.getSpotClearinghouseState(walletAddress, true);
let usdcBalance = 0;
for (const bal of spotState.balances) {
  if (bal.coin === 'USDC') {
    usdcBalance = parseFloat(bal.total);
    break;
  }
}
console.log(`USDC Balance: $${usdcBalance.toFixed(2)}`);
```

---

## 5. Colocar Ordens

### 5.1 Helpers de rounding (OBRIGATÓRIO)

A HL rejeita ordens com preço/tamanho fora da precisão esperada. Sempre arredondar:

```typescript
/** Arredondar tamanho conforme szDecimals do ativo */
function roundSize(size: number, szDecimals: number): number {
  const factor = Math.pow(10, szDecimals);
  return Math.round(size * factor) / factor;
}

/**
 * Arredondar preço para formato HL.
 * Regra: 5 significant figures, max (6 - szDecimals) casas decimais.
 * Garante compliance com tick size.
 */
function roundPrice(price: number, szDecimals: number): number {
  const sig5 = parseFloat(price.toPrecision(5));
  const maxDec = Math.max(0, 6 - szDecimals);
  return Number(sig5.toFixed(maxDec));
}
```

### 5.2 Ordem Market (IOC) — DEX padrão

A HL não tem tipo "market" nativo. A prática é enviar uma **limit IOC** (Immediate-or-Cancel) com slippage:

```typescript
const DEFAULT_SLIPPAGE = 0.005; // 0.5%

async function marketSell(sdk: Hyperliquid, coin: string, size: number, szDecimals: number) {
  // Obter preço mid atual
  const allMids = await sdk.info.getAllMids(true) as Record<string, string>;
  const midPrice = parseFloat(allMids[coin]);

  // Sell = abrir short → preço com slippage para baixo
  const slippagePrice = midPrice * (1 - DEFAULT_SLIPPAGE);
  const roundedPx = roundPrice(slippagePrice, szDecimals);
  const roundedSz = roundSize(size, szDecimals);

  const result = await sdk.exchange.placeOrder({
    coin: `${coin}-PERP`,     // SDK espera formato "BTC-PERP"
    is_buy: false,             // false = sell (abrir/aumentar short)
    sz: roundedSz,
    limit_px: roundedPx,
    order_type: { limit: { tif: 'Ioc' } },  // IOC = executa imediato ou cancela
    reduce_only: false,
  });

  return result;
}

async function marketBuy(sdk: Hyperliquid, coin: string, size: number, szDecimals: number) {
  const allMids = await sdk.info.getAllMids(true) as Record<string, string>;
  const midPrice = parseFloat(allMids[coin]);

  // Buy = fechar short ou abrir long → preço com slippage para cima
  const slippagePrice = midPrice * (1 + DEFAULT_SLIPPAGE);
  const roundedPx = roundPrice(slippagePrice, szDecimals);
  const roundedSz = roundSize(size, szDecimals);

  const result = await sdk.exchange.placeOrder({
    coin: `${coin}-PERP`,
    is_buy: true,
    sz: roundedSz,
    limit_px: roundedPx,
    order_type: { limit: { tif: 'Ioc' } },
    reduce_only: false,       // true se quiser APENAS reduzir posição existente
  });

  return result;
}
```

### 5.3 Fechar posição inteira

```typescript
async function closeShort(sdk: Hyperliquid, coin: string, currentSize: number, szDecimals: number) {
  const allMids = await sdk.info.getAllMids(true) as Record<string, string>;
  const midPrice = parseFloat(allMids[coin]);
  const slippagePrice = midPrice * (1 + DEFAULT_SLIPPAGE); // buy to close short
  const roundedPx = roundPrice(slippagePrice, szDecimals);

  const result = await sdk.exchange.placeOrder({
    coin: `${coin}-PERP`,
    is_buy: true,
    sz: currentSize,
    limit_px: roundedPx,
    order_type: { limit: { tif: 'Ioc' } },
    reduce_only: true,  // IMPORTANTE: reduce_only = true para fechar
  });

  return result;
}
```

### 5.4 Ordem Limit (GTC — Good Till Cancel)

```typescript
async function limitSell(sdk: Hyperliquid, coin: string, size: number, price: number, szDecimals: number) {
  const roundedPx = roundPrice(price, szDecimals);
  const roundedSz = roundSize(size, szDecimals);

  const result = await sdk.exchange.placeOrder({
    coin: `${coin}-PERP`,
    is_buy: false,
    sz: roundedSz,
    limit_px: roundedPx,
    order_type: { limit: { tif: 'Gtc' } },  // GTC = fica no book até executar ou cancelar
    reduce_only: false,
  });

  return result;
}
```

**Tipos de Time-in-Force:**

| TIF | Descrição |
|-----|-----------|
| `Ioc` | Immediate-or-Cancel — executa o que conseguir, cancela o resto |
| `Gtc` | Good-Till-Cancel — fica no book até preencher ou ser cancelada |
| `Alo` | Add-Liquidity-Only — só executa como maker (rejeita se tomaria liquidez) |

### 5.5 Ordens HIP-3 (xyz, cash) — bypassa o SDK

O SDK **não suporta** o campo `dex` para HIP-3. Você precisa construir e assinar a ação raw:

```typescript
async function hip3MarketSell(
  sdk: Hyperliquid,
  assetIndex: number,    // Index global (ex: 110013 para xyz:AMZN)
  coin: string,          // "xyz:AMZN"
  dex: string,           // "xyz"
  size: number,
  szDecimals: number,
) {
  // Obter mid price do dex específico
  const [, ctxs] = await hlInfo('metaAndAssetCtxs', { dex }) as [any, Array<{ midPx: string }>];
  // Precisa saber o localIndex do asset dentro do dex
  const localIndex = assetIndex % 10000; // offset remove o prefixo
  const midPrice = parseFloat(ctxs[localIndex].midPx);

  const slippagePrice = midPrice * (1 - DEFAULT_SLIPPAGE);
  const roundedPx = roundPrice(slippagePrice, szDecimals);
  const roundedSz = roundSize(size, szDecimals);

  const pxStr = floatToWire(roundedPx);
  const szStr = floatToWire(roundedSz);

  // Construir ação raw
  const orderWire = {
    a: assetIndex,     // Index global com offset do dex
    b: false,          // false = sell
    p: pxStr,
    s: szStr,
    r: false,          // reduce_only
    t: { limit: { tif: 'Ioc' as const } },
  };
  const action = {
    type: 'order' as const,
    orders: [orderWire],
    grouping: 'na' as const,
  };

  // Assinar e submeter
  // NOTA: precisa acessar membros privados do SDK
  const exchange = (sdk as any).exchange;
  const wallet = exchange.wallet as ethers.Wallet;
  const httpApi = exchange.httpApi;
  const vaultAddress = exchange.vaultAddress ?? null;
  const nonce = Date.now();

  const signature = await signL1Action(
    wallet,
    action,
    vaultAddress,
    nonce,
    true, // IS_MAINNET (false para testnet)
  );

  const payload = { action, nonce, signature, vaultAddress };
  return httpApi.makeRequest(payload, 1);
}
```

**Cálculo de offsets HIP-3:**

```typescript
// Obter offsets dos DEXes
const perpDexList = await hlInfo('perpDexs') as Array<{ name: string } | null>;
const dexOffsets = new Map<string, number>();
dexOffsets.set('', 0);  // DEX padrão
let hip3Idx = 0;
for (const entry of perpDexList) {
  if (entry === null) continue; // skip default dex
  dexOffsets.set(entry.name, 110000 + hip3Idx * 10000);
  hip3Idx++;
}
// Resultado: { "": 0, "xyz": 110000, "cash": 120000 }

// Asset index global = offset + índice local
// Se AMZN é o 13º asset no xyz dex: assetIndex = 110000 + 13 = 110013
```

### 5.6 Interpretar resultado da ordem

```typescript
function parseOrderResult(result: any): { filled: boolean; avgPx: number; totalSz: number } {
  const statuses = result?.response?.data?.statuses ?? result?.statuses;

  if (!statuses || statuses.length === 0) {
    throw new Error('Sem status na resposta da HL');
  }

  const status = statuses[0];

  if (status.filled) {
    return {
      filled: true,
      avgPx: parseFloat(status.filled.avgPx),
      totalSz: parseFloat(status.filled.totalSz),
    };
  }

  if (status.error) {
    throw new Error(`Ordem rejeitada: ${JSON.stringify(status.error)}`);
  }

  if (status.resting) {
    // Ordem limit GTC foi para o book (não executou imediatamente)
    return { filled: false, avgPx: 0, totalSz: 0 };
  }

  throw new Error(`Status inesperado: ${JSON.stringify(status)}`);
}
```

---

## 6. Funding Rate

### 6.1 Obter funding rate atual

```typescript
async function getFundingRate(coin: string, dex?: string): Promise<number> {
  const params = dex ? { dex } : {};
  const [meta, contexts] = await hlInfo('metaAndAssetCtxs', params) as [
    { universe: Array<{ name: string }> },
    Array<{ funding: string }>,
  ];

  const index = meta.universe.findIndex(a => a.name === coin);
  if (index === -1) throw new Error(`Asset ${coin} não encontrado`);

  const rate = parseFloat(contexts[index].funding);
  // rate é HORÁRIO. Para anualizar: rate * 24 * 365
  console.log(`Funding ${coin}: ${(rate * 100).toFixed(4)}%/h (${(rate * 24 * 365 * 100).toFixed(2)}%/ano)`);
  return rate;
}
```

> **Nota:** Funding na HL é pago a cada hora (8760 pagamentos/ano). Rate positivo = shorts recebem, longs pagam.

### 6.2 Histórico de funding recebido/pago

```typescript
async function getUserFunding(
  sdk: Hyperliquid,
  walletAddress: string,
  coin: string,
  sinceTimestamp: number, // ms
  dex?: string,
): Promise<number> {
  let fundingData: Array<{ delta?: { coin?: string; usdc?: string }; coin?: string; usdc?: string }>;

  if (dex) {
    // HIP-3
    fundingData = await hlInfo('userFunding', {
      user: walletAddress,
      startTime: sinceTimestamp,
      dex,
    }) as typeof fundingData;
  } else {
    // DEX padrão
    fundingData = await (sdk.info.perpetuals as any).getUserFunding(walletAddress, sinceTimestamp) ?? [];
  }

  // IMPORTANTE: resposta tem wrapper .delta
  // Estrutura: [{ delta: { coin, usdc, szi, fundingRate }, hash, time }]
  const coinFunding = (Array.isArray(fundingData) ? fundingData : []).filter(f => {
    const fc = f.delta?.coin ?? f.coin ?? '';
    return fc === coin;
  });

  const totalFunding = coinFunding.reduce(
    (sum, f) => sum + parseFloat(f.delta?.usdc ?? f.usdc ?? '0'), 0
  );

  return totalFunding; // Positivo = recebeu funding, negativo = pagou
}
```

---

## 7. Histórico de Trades (Fills)

```typescript
async function getUserFills(
  sdk: Hyperliquid,
  walletAddress: string,
  coin: string,
  sinceTimestamp: number,
) {
  await sdk.ensureInitialized();

  const fills = await (sdk.info as any).getUserFillsByTime(walletAddress, sinceTimestamp);
  const allFills: Array<Record<string, string>> = Array.isArray(fills) ? fills : [];

  // Filtrar por coin
  const coinFills = allFills.filter(f => (f.coin ?? '') === coin);

  // P&L realizado (soma de closedPnl de cada fill)
  const realizedPnl = coinFills.reduce((sum, f) => sum + parseFloat(f.closedPnl ?? '0'), 0);

  // Fees pagas
  const totalFees = coinFills.reduce((sum, f) => sum + parseFloat(f.fee ?? '0'), 0);

  return { fills: coinFills, realizedPnl, totalFees };
}
```

---

## 8. Exemplo Completo — Bot Mínimo

```typescript
import { Hyperliquid } from 'hyperliquid';

const SLIPPAGE = 0.005;

async function main() {
  // 1. Conectar
  const sdk = new Hyperliquid({
    privateKey: process.env.HL_PRIVATE_KEY!,
    walletAddress: process.env.HL_WALLET_ADDRESS!,
    enableWs: false,
  });
  await sdk.ensureInitialized();

  const wallet = process.env.HL_WALLET_ADDRESS!;

  // 2. Listar mercados
  const [meta, contexts] = await fetch('https://api.hyperliquid.xyz/info', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ type: 'metaAndAssetCtxs' }),
  }).then(r => r.json()) as [
    { universe: Array<{ name: string; szDecimals: number }> },
    Array<{ midPx: string; funding: string }>,
  ];

  // 3. Encontrar ETH
  const ethIdx = meta.universe.findIndex(a => a.name === 'ETH');
  const ethMeta = meta.universe[ethIdx];
  const ethCtx = contexts[ethIdx];
  const midPrice = parseFloat(ethCtx.midPx);
  console.log(`ETH mid: $${midPrice}, funding: ${ethCtx.funding}, szDecimals: ${ethMeta.szDecimals}`);

  // 4. Ver posição atual
  const state = await sdk.info.perpetuals.getClearinghouseState(wallet, true);
  const ethPos = state.assetPositions.find(ap => String(ap.position.coin) === 'ETH');
  if (ethPos) {
    console.log(`Posição ETH: size=${ethPos.position.szi}, pnl=${ethPos.position.unrealizedPnl}`);
  }

  // 5. Abrir short de 0.1 ETH
  const sz = 0.1;
  const sellPx = parseFloat((midPrice * (1 - SLIPPAGE)).toPrecision(5));

  const result = await sdk.exchange.placeOrder({
    coin: 'ETH-PERP',
    is_buy: false,
    sz,
    limit_px: sellPx,
    order_type: { limit: { tif: 'Ioc' } },
    reduce_only: false,
  });

  const status = result?.response?.data?.statuses?.[0];
  if (status?.filled) {
    console.log(`Short aberto: ${status.filled.totalSz} ETH @ $${status.filled.avgPx}`);
  }

  // 6. Loop de monitoramento
  setInterval(async () => {
    const st = await sdk.info.perpetuals.getClearinghouseState(wallet, true);
    const pos = st.assetPositions.find(ap => String(ap.position.coin) === 'ETH');
    if (pos) {
      console.log(`ETH: size=${pos.position.szi} unrealizedPnl=${pos.position.unrealizedPnl}`);
    }
  }, 30_000); // a cada 30s
}

main().catch(console.error);
```

---

## 9. Referência Rápida — Endpoints da Info API

| `type` | Parâmetros | Retorna |
|--------|-----------|---------|
| `metaAndAssetCtxs` | `{ dex? }` | [meta, contexts] — universo de ativos + preços/funding |
| `clearinghouseState` | `{ user, dex? }` | Posições abertas, margem, PnL |
| `allMids` | — | `{ "BTC": "67000", ... }` (só DEX padrão) |
| `perpDexs` | — | Lista de DEXes: `[null, {name:"xyz"}, {name:"cash"}]` |
| `userFunding` | `{ user, startTime, dex? }` | Histórico de funding |

**Métodos do SDK (DEX padrão):**

| Método | Descrição |
|--------|-----------|
| `sdk.info.getAllMids(true)` | Todos os mid prices |
| `sdk.info.perpetuals.getClearinghouseState(addr, true)` | Posições abertas |
| `sdk.info.getUserFillsByTime(addr, sinceMs)` | Histórico de fills |
| `sdk.info.perpetuals.getUserFunding(addr, sinceMs)` | Histórico de funding |
| `sdk.info.spot.getSpotClearinghouseState(addr, true)` | Saldo USDC |
| `sdk.exchange.placeOrder({...})` | Colocar ordem |

---

## 10. Dicas e Gotchas

1. **Sempre `ensureInitialized()`** antes da primeira operação — o SDK carrega metadata internamente.

2. **SDK não suporta HIP-3** — para assets `xyz:*` e `cash:*`, use `signL1Action` + fetch direto.

3. **Rounding é obrigatório** — a HL rejeita ordens com preço/tamanho fora da precisão. Use `roundSize()` e `roundPrice()` sempre.

4. **`coin` no SDK tem sufixo `-PERP`** — `sdk.exchange.placeOrder` espera `"ETH-PERP"`, mas `getClearinghouseState` retorna `"ETH"`.

5. **`szi` negativo = short** — na resposta de `clearinghouseState`, `szi` é signed: negativo indica posição short.

6. **Funding wrapper `.delta`** — a resposta de `userFunding` tem estrutura `[{ delta: { coin, usdc }, time }]`. Acessar `f.delta.usdc`, nunca `f.usdc` diretamente.

7. **Rate limits** — a API info não tem rate limit agressivo, mas evite polling mais que 1x/segundo. A API exchange tem limites mais rigorosos.

8. **Testnet** — para testes, use `IS_MAINNET = false` no `signL1Action`. O SDK também suporta testnet via configuração.

9. **`reduce_only: true`** — use ao fechar posições para evitar abrir posição no sentido oposto acidentalmente.

10. **IOC vs GTC** — para "market orders", use IOC com slippage. GTC fica no book e pode não executar imediatamente.
