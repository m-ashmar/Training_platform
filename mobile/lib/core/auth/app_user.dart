enum UserRole {
  client,
  trainer,
  agent,
  admin,
  unknown;

  static UserRole fromWire(Object? value) {
    return switch (value) {
      'client' => client,
      'trainer' => trainer,
      'agent' => agent,
      'admin' => admin,
      _ => unknown,
    };
  }

  String get wire => name;
}

/// The signed-in user as the app sees it. Decoded in one place; unknown
/// wire fields are ignored (the contract is additive).
class AppUser {
  const AppUser({
    required this.id,
    required this.username,
    required this.email,
    required this.firstName,
    required this.lastName,
    required this.role,
    this.profilePictureUrl,
    this.onboardingCompleted = false,
  });

  factory AppUser.fromJson(Map<String, dynamic> json) {
    return AppUser(
      id: (json['id'] as num).toInt(),
      username: json['username'] as String? ?? '',
      email: json['email'] as String? ?? '',
      firstName: json['first_name'] as String? ?? '',
      lastName: json['last_name'] as String? ?? '',
      role: UserRole.fromWire(json['user_type']),
      profilePictureUrl: json['profile_picture'] as String?,
      onboardingCompleted: json['onboarding_completed'] == true,
    );
  }

  final int id;
  final String username;
  final String email;
  final String firstName;
  final String lastName;
  final UserRole role;

  /// Signed and expiring; never persist it beyond the cached user record.
  final String? profilePictureUrl;
  final bool onboardingCompleted;

  String get displayName => firstName.isNotEmpty ? firstName : username;

  bool get isTrainer => role == UserRole.trainer;

  Map<String, dynamic> toJson() => <String, dynamic>{
        'id': id,
        'username': username,
        'email': email,
        'first_name': firstName,
        'last_name': lastName,
        'user_type': role.wire,
        'profile_picture': profilePictureUrl,
        'onboarding_completed': onboardingCompleted,
      };
}
