//+------------------------------------------------------------------+
//|                                                  ForexProEA.mq5   |
//|  Bridges a real MetaTrader 5 account to your ForexPro backend.   |
//|                                                                    |
//|  SETUP (do this before running):                                  |
//|  1. Compile: open this file in MetaEditor (F4 in MT5), press F7.  |
//|  2. In MT5: Tools > Options > Expert Advisors > tick "Allow        |
//|     WebRequest for listed URL" and ADD your BridgeURL (below) to   |
//|     the list — e.g. http://localhost:8766 or your deployed         |
//|     Railway/Render URL. MT5 refuses WebRequest to any URL not on   |
//|     that list; this is an MT5 security feature, not a bug here.    |
//|  3. Drag this EA onto any chart, fill in BridgeURL + BridgeToken   |
//|     (from your ForexPro Profile page), then enable AutoTrading.    |
//|  4. Only trades where you (or a provider you follow with           |
//|     "execute live" on) chose real MT5 execution get sent here —    |
//|     everything else stays a simulated in-app copy trade.           |
//|                                                                    |
//|  NOTE: this EA keeps its list of open bridge trades in memory      |
//|  only — if you restart the EA/terminal mid-trade, it stops        |
//|  watching that position for a close report (the position itself   |
//|  is completely safe, MT5 still manages it normally; ForexPro just  |
//|  won't hear about the close automatically). Re-attaching the EA    |
//|  does not re-open or duplicate anything.                          |
//+------------------------------------------------------------------+
#property copyright "Yobby Technologies"
#property version   "1.00"
#property strict

#include <Trade\Trade.mqh>
CTrade trade;

input string InpBridgeURL     = "http://localhost:8766"; // Backend base URL (no trailing slash)
input string InpBridgeToken   = "";                      // Bridge token from your Profile page
input int    InpPollSeconds   = 5;                        // How often to poll for new orders
input string InpSymbolSuffix  = "";                        // Broker symbol suffix, e.g. ".a" or "m" (leave blank if none)
input int    InpSlippagePts   = 30;                        // Max allowed slippage, in points
input double InpMagicNumber   = 990011;                    // Magic number for orders placed by this EA

struct BridgeTrade
  {
   long   copyTradeId;
   ulong  ticket;
   string symbol;
   double lot;
   double entryPrice;
   string direction;
  };
BridgeTrade g_open[];

//+------------------------------------------------------------------+
int OnInit()
  {
   if(StringLen(InpBridgeToken) == 0)
     {
      Print("ForexProEA: BridgeToken is empty — paste the token from your ForexPro Profile page.");
      return(INIT_PARAMETERS_INCORRECT);
     }
   trade.SetExpertMagicNumber((int)InpMagicNumber);
   trade.SetDeviationInPoints(InpSlippagePts);
   EventSetTimer(MathMax(InpPollSeconds, 2));
   Print("ForexProEA: started, polling ", InpBridgeURL, " every ", InpPollSeconds, "s");
   return(INIT_SUCCEEDED);
  }

void OnDeinit(const int reason) { EventKillTimer(); }

//+------------------------------------------------------------------+
//| Minimal HTTP helpers — WebRequest needs the URL pre-approved in   |
//| MT5's Options dialog (see header comment above).                  |
//+------------------------------------------------------------------+
string HttpRequest(string method, string url)
  {
   char post[]; char result[]; string resultHeaders;
   ResetLastError();
   int status = WebRequest(method, url, "", 5000, post, result, resultHeaders);
   if(status == -1)
     {
      int err = GetLastError();
      if(err == 4060)
         Print("ForexProEA: WebRequest blocked — add ", InpBridgeURL, " to Tools>Options>Expert Advisors>Allow WebRequest.");
      else
         Print("ForexProEA: WebRequest failed, error ", err, " for ", url);
      return "";
     }
   if(status != 200)
     {
      Print("ForexProEA: HTTP ", status, " from ", url);
      return "";
     }
   return CharArrayToString(result);
  }

string UrlEncode(string s)
  {
   // Bridge tokens/symbols used here are alphanumeric + underscores, so no
   // real encoding is needed — kept as a no-op hook in case that changes.
   return s;
  }

//+------------------------------------------------------------------+
void OnTimer()
  {
   HttpRequest("POST", InpBridgeURL + "/bridge/heartbeat?token=" + UrlEncode(InpBridgeToken));
   PollPendingOrders();
   CheckClosedPositions();
  }

//+------------------------------------------------------------------+
//| Poll for new copy-trades waiting to be executed on this account   |
//+------------------------------------------------------------------+
void PollPendingOrders()
  {
   string body = HttpRequest("GET", InpBridgeURL + "/bridge/pending-orders?token=" + UrlEncode(InpBridgeToken));
   if(StringLen(body) == 0) return;

   string lines[];
   int n = StringSplit(body, '\n', lines);
   for(int i = 0; i < n; i++)
     {
      string line = lines[i];
      StringTrimLeft(line); StringTrimRight(line);
      if(StringLen(line) == 0) continue;

      string parts[];
      int pc = StringSplit(line, '|', parts);
      if(pc < 7)
        {
         Print("ForexProEA: malformed line from bridge, skipping: ", line);
         continue;
        }

      long   copyTradeId = StringToInteger(parts[0]);
      string symbol      = parts[1] + InpSymbolSuffix;
      string direction   = parts[2];
      double lot         = StringToDouble(parts[3]);
      double sl          = StringToDouble(parts[5]);
      double tp          = StringToDouble(parts[6]);

      ExecuteOrder(copyTradeId, symbol, direction, lot, sl, tp);
     }
  }

void ExecuteOrder(long copyTradeId, string symbol, string direction, double lot, double sl, double tp)
  {
   if(!SymbolSelect(symbol, true))
     {
      ReportFill(copyTradeId, "failed", 0, 0, "Symbol " + symbol + " not found — check InpSymbolSuffix");
      return;
     }

   bool ok;
   if(direction == "BUY")
      ok = trade.Buy(lot, symbol, 0, sl, tp, "ForexPro #" + IntegerToString(copyTradeId));
   else
      ok = trade.Sell(lot, symbol, 0, sl, tp, "ForexPro #" + IntegerToString(copyTradeId));

   if(!ok)
     {
      ReportFill(copyTradeId, "failed", 0, 0, "MT5 error " + IntegerToString(trade.ResultRetcode()));
      return;
     }

   ulong ticket = trade.ResultOrder();
   double fillPrice = trade.ResultPrice();
   ReportFill(copyTradeId, "filled", ticket, fillPrice, "");

   int sz = ArraySize(g_open);
   ArrayResize(g_open, sz + 1);
   g_open[sz].copyTradeId = copyTradeId;
   g_open[sz].ticket      = ticket;
   g_open[sz].symbol      = symbol;
   g_open[sz].lot         = lot;
   g_open[sz].entryPrice  = fillPrice;
   g_open[sz].direction   = direction;
  }

void ReportFill(long copyTradeId, string status, ulong ticket, double fillPrice, string errMsg)
  {
   string url = InpBridgeURL + "/bridge/report-fill?token=" + UrlEncode(InpBridgeToken)
              + "&copy_trade_id=" + IntegerToString((int)copyTradeId)
              + "&status=" + status
              + "&ticket=" + IntegerToString((long)ticket)
              + "&fill_price=" + DoubleToString(fillPrice, 5)
              + "&error_msg=" + UrlEncode(errMsg);
   HttpRequest("POST", url);
  }

//+------------------------------------------------------------------+
//| Watch positions we opened; report to the backend the moment MT5   |
//| closes one (TP/SL hit or manual close) with the real P&L.         |
//+------------------------------------------------------------------+
void CheckClosedPositions()
  {
   for(int i = ArraySize(g_open) - 1; i >= 0; i--)
     {
      ulong ticket = g_open[i].ticket;
      if(PositionSelectByTicket(ticket)) continue; // still open, nothing to do

      double closePrice = 0, pnlUsd = 0;
      if(HistorySelectByPosition(ticket))
        {
         int deals = HistoryDealsTotal();
         for(int d = 0; d < deals; d++)
           {
            ulong dealTicket = HistoryDealGetTicket(d);
            if(HistoryDealGetInteger(dealTicket, DEAL_ENTRY) == DEAL_ENTRY_OUT)
              {
               closePrice = HistoryDealGetDouble(dealTicket, DEAL_PRICE);
               pnlUsd    += HistoryDealGetDouble(dealTicket, DEAL_PROFIT)
                          + HistoryDealGetDouble(dealTicket, DEAL_SWAP)
                          + HistoryDealGetDouble(dealTicket, DEAL_COMMISSION);
              }
           }
        }

      double point = SymbolInfoDouble(g_open[i].symbol, SYMBOL_POINT);
      int    digits = (int)SymbolInfoInteger(g_open[i].symbol, SYMBOL_DIGITS);
      double pip = (digits == 3 || digits == 5) ? point * 10 : point;
      double dirMul = (g_open[i].direction == "BUY") ? 1.0 : -1.0;
      double pnlPips = (closePrice > 0 && g_open[i].entryPrice > 0)
                      ? (closePrice - g_open[i].entryPrice) / pip * dirMul
                      : 0;
      string result = (pnlUsd > 0) ? "win" : (pnlUsd < 0 ? "loss" : "breakeven");

      string url = InpBridgeURL + "/bridge/report-close?token=" + UrlEncode(InpBridgeToken)
                 + "&copy_trade_id=" + IntegerToString((int)g_open[i].copyTradeId)
                 + "&ticket=" + IntegerToString((long)ticket)
                 + "&close_price=" + DoubleToString(closePrice, 5)
                 + "&pnl_usd=" + DoubleToString(pnlUsd, 2)
                 + "&pnl_pips=" + DoubleToString(pnlPips, 1)
                 + "&result=" + result;
      HttpRequest("POST", url);

      ArrayRemove(g_open, i, 1);
     }
  }
//+------------------------------------------------------------------+
