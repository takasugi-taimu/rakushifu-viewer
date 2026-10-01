class StaffNotFound(Exception):
    pass


class CredentialsUnavailable(Exception):
    pass


class UpstreamError(Exception):
    pass


class InvalidScheduleData(Exception):
    pass


class InvalidCredentials(Exception):
    pass


class SessionExpired(Exception):
    pass


class StorageUnavailable(Exception):
    pass
