import logging
import datetime
import pandas as pd

import pyalgomate.utils as utils
from pyalgomate.strategies.BaseOptionsGreeksStrategy import BaseOptionsGreeksStrategy
from pyalgomate.strategies.BaseOptionsGreeksStrategy import State, Expiry
from pyalgomate.cli import CliMain

logger = logging.getLogger(__file__)


class DeltaNeutralIntradayV2(BaseOptionsGreeksStrategy):
    def __init__(self, feed, broker, strategyName=None, registeredOptionsCount=None, callback=None, resampleFrequency=None, lotSize=None, collectData=None, telegramBot=None):
        super(DeltaNeutralIntradayV2, self).__init__(feed, broker,
                                                   strategyName=strategyName if strategyName else __class__.__name__,
                                                   logger=logging.getLogger(
                                                       __file__),
                                                   callback=callback,
                                                   resampleFrequency=resampleFrequency,
                                                   collectData=collectData,
                                                   telegramBot=telegramBot)

        self.entryTime = datetime.time(hour=9, minute=17)
        self.exitTime = datetime.time(hour=15, minute=15)
        self.expiry = Expiry.WEEKLY
        self.initialDeltaDifference = 0.2
        self.deltaThreshold = 0.2
        self.lotSize = lotSize if lotSize is not None else 25
        self.lots = 1
        self.quantity = self.lotSize * self.lots
        self.portfolioSL = 2000

        self.registeredOptionsCount = registeredOptionsCount if registeredOptionsCount is not None else 0

        self.__reset__()

    def __reset__(self):
        super().reset()
        # members that needs to be reset after exit time
        self.positionCall = None
        self.positionPut = None
        self.positionVega = None
        self.numberOfAdjustments = 0
        self.pendingPositions = dict()

    def closeAllPositions(self):
        self.state = State.EXITED
        for position in list(self.getActivePositions()):
            if not position.exitActive():
                position.exitMarket()

        self.positionCall = self.positionPut = self.positionVega = None

    def onStart(self):
        super().onStart()

        for activePosition in list(self.getActivePositions()):
            if activePosition.getEntryOrder().isBuy():
                self.positionVega = activePosition
            else:
                optionContract = self.getBroker().getOptionContract(activePosition.getInstrument())
                if optionContract is not None:
                    if optionContract.type == 'c':
                        self.positionCall = activePosition
                    else:
                        self.positionPut = activePosition

        if self.positionCall is not None and self.positionPut is not None:
            self.state = State.ENTERED

    def onEnterCanceled(self, position):
        super().onEnterCanceled(position)

        if self.positionCall is not None and (self.positionCall.getInstrument() == position.getInstrument()):
            self.positionCall = None
        elif self.positionPut is not None and (self.positionPut.getInstrument() == position.getInstrument()):
            self.positionPut = None
        elif self.positionVega is not None and (self.positionVega.getInstrument() == position.getInstrument()):
            self.positionVega = None

    def onExitCanceled(self, position):
        super().onExitCanceled(position)

        if self.positionCall is not None and (self.positionCall.getInstrument() == position.getInstrument()):
            self.positionCall = None
        elif self.positionPut is not None and (self.positionPut.getInstrument() == position.getInstrument()):
            self.positionPut = None
        elif self.positionVega is not None and (self.positionVega.getInstrument() == position.getInstrument()):
            self.positionVega = None

    def shouldDoAdjustments(self, optionData):
        # Adjust positions if any of the price has become thrice of the other
        callLTP = self.getLTP(self.positionCall.getInstrument(
        ))
        putLTP = self.getLTP(self.positionPut.getInstrument(
        ))

        if (callLTP > 3 * putLTP) or (putLTP > 3 * callLTP):
            return True, optionData[self.positionCall.getInstrument(
            )], optionData[self.positionPut.getInstrument()]

        return False, optionData[self.positionCall.getInstrument(
        )], optionData[self.positionPut.getInstrument()]

    def takePositions(self, expiry):
        selectedCallOption = self.getNearestDeltaOption(
            'c', self.initialDeltaDifference, expiry)
        selectedPutOption = self.getNearestDeltaOption(
            'p', self.initialDeltaDifference, expiry)

        if selectedCallOption is None or selectedPutOption is None:
            return

        # Return if we do not have LTP for selected options yet
        if not (self.haveLTP(selectedCallOption.optionContract.symbol) and self.haveLTP(selectedPutOption.optionContract.symbol)):
            return

        if self.state == State.PLACING_ORDERS:
            return

        self.state = State.PLACING_ORDERS
        # Place initial delta-neutral positions
        self.positionCall = self.enterShort(
            selectedCallOption.optionContract.symbol, self.quantity)
        self.positionPut = self.enterShort(
            selectedPutOption.optionContract.symbol, self.quantity)

    def onBars(self, bars):
        self.log(f"Bar date times - {bars.getDateTime()}", logging.DEBUG)
        overallDelta = self.getOverallDelta()

        currentExpiry = utils.getNearestWeeklyExpiryDate(bars.getDateTime().date(
        )) if self.expiry == Expiry.WEEKLY else utils.getNearestMonthlyExpiryDate(bars.getDateTime().date())

        optionData = self.getOptionData(bars)

        if (len(optionData) < self.registeredOptionsCount):
            return

        self.overallPnL = self.getOverallPnL()

        self.log(f"Current PnL is {self.overallPnL}. Overall delta is {overallDelta}. Datetime {bars.getDateTime()}. State is {State.toString(self.state)}.\n" +
                 "\tRegistered option count is {self.registeredOptionsCount}. Number of options present {len(optionData)}.\n" +
                 "\tNumber of open positions are {len(self.openPositions)}. Number of closed positions are {len(self.closedPositions)}.", logging.DEBUG)

        # Check if the market has closed
        if bars.getDateTime().time() >= self.marketEndTime:
            if (len(self.openPositions) + len(self.closedPositions)) > 0:
                self.log(
                    f"Overall PnL for {bars.getDateTime().date()} is {self.overallPnL}")
            if self.state != State.LIVE:
                self.__reset__()
        elif self.overallPnL <= -self.portfolioSL:
            self.log(
                f"Portfolio SL({self.portfolioSL} is hit. Current PnL is {self.overallPnL}. Exiting all positions!)")
            if self.state != State.EXITED:
                self.closeAllPositions()
        elif self.state == State.LIVE:
            if bars.getDateTime().time() >= self.entryTime and bars.getDateTime().time() < self.exitTime:
                self.takePositions(currentExpiry)
        # Exit all positions if exit time is met or portfolio SL is hit
        elif bars.getDateTime().time() >= self.exitTime:
            if self.state != State.EXITED:
                self.closeAllPositions()
        elif self.state == State.PLACING_ORDERS:
            if len(list(self.getActivePositions())) == 0:
                self.state = State.LIVE
                return
            if self.isPendingOrdersCompleted():
                self.state = State.ENTERED
                return
        elif self.state == State.ENTERED:
            shouldDoAdjustments, callOptionGreeks, putOptionGreeks = self.shouldDoAdjustments(optionData)

            if shouldDoAdjustments:
                if (abs(callOptionGreeks.delta) > 0.45) or (abs(putOptionGreeks.delta) > 0.45):
                    self.closeAllPositions()
                    self.takePositions(currentExpiry)
                    self.numberOfAdjustments = 0
                # Close the profit making position and take another position with delta nearest to that of other option
                elif abs(callOptionGreeks.delta) > abs(putOptionGreeks.delta):
                    self.positionPut.exitMarket()
                    # Find put option with delta closest to delta of put option
                    selectedPutOption = self.getNearestDeltaOption(
                        'p', callOptionGreeks.delta, currentExpiry)

                    self.state = State.PLACING_ORDERS
                    self.positionPut = self.enterShort(
                        selectedPutOption.optionContract.symbol, self.quantity)
                    self.numberOfAdjustments += 1
                else:
                    self.positionCall.exitMarket()
                    # Find call option with delta closest to delta of call option
                    selectedCallOption = self.getNearestDeltaOption(
                        'c', putOptionGreeks.delta, currentExpiry)
                    self.state = State.PLACING_ORDERS
                    self.positionCall = self.enterShort(
                        selectedCallOption.optionContract.symbol, self.quantity)
                    self.numberOfAdjustments -= 1

            if self.positionVega is None and abs(self.numberOfAdjustments) >= 2:
                selectedOption = self.getNearestDeltaOption('c' if abs(
                    callOptionGreeks.delta) > abs(putOptionGreeks.delta) else 'p', 0.5, currentExpiry)
                if selectedOption.optionContract.symbol in [self.positionCall.getInstrument(),
                                                            self.positionPut.getInstrument()]:
                    self.log(
                        f"We just have entered short positon of <{selectedOption.optionContract.symbol}> in current adjustment. Skipping buying same position.")
                else:
                    self.log(
                        f"Number of adjustments has reached {self.numberOfAdjustments}. Managing vega by buying an option. Current PnL is {self.overallPnL}).")
                    self.state = State.PLACING_ORDERS
                    self.positionVega = self.enterLong(
                        selectedOption.optionContract.symbol, self.quantity)
        # Check if we are in the EXITED state
        elif self.state == State.EXITED:
            pass


if __name__ == "__main__":
    CliMain(DeltaNeutralIntradayV2)
