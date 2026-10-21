package fixture.before;

public class OrderController {
    private final OrderApplication orderApplication;

    public OrderController(OrderApplication orderApplication) {
        this.orderApplication = orderApplication;
    }

    public Boolean save(OrderSaveReq req) {
        return orderApplication.save(req.getOrderNo(), req.getRemark());
    }
}
