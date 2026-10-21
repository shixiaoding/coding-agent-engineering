package fixture.before;

public class StoreProgressService {
    public StoreProgressQueryResult
            queryStoreProgressByTaskAndOrganizationScopeWithCurrentUserPermissions(
                    StoreProgressQueryRequest request) {
        return new StoreProgressQueryResult();
    }

    static class StoreProgressQueryRequest {
    }

    static class StoreProgressQueryResult {
    }
}
